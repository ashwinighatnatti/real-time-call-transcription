"""Live call monitor: mic + system audio -> Azure Speech -> LLM extraction -> form -> SQLite.

Run locally on Windows:  python app.py   then open http://127.0.0.1:5000
Fields are defined in fields.py. Secrets come from .env (see .env.example).
"""
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from difflib import SequenceMatcher

from dotenv import load_dotenv

load_dotenv()

_missing = [k for k in ("AZURE_ANTHROPIC_API_KEY", "AZURE_ANTHROPIC_API_ENDPOINT") if not os.environ.get(k)]
if not (os.environ.get("MODEL_DEPLOYMENT") or os.environ.get("AZURE_ANTHROPIC_DEPLOYMENT_NAME")):
    _missing.append("MODEL_DEPLOYMENT")
if _missing:
    sys.exit(f"Missing environment variables: {', '.join(_missing)}. Copy .env.example to .env and fill it in.")

import azure.cognitiveservices.speech as speechsdk
import pyaudiowpatch as pyaudio  # PyAudio fork with WASAPI loopback (captures the remote caller)
from flask import Flask, Response, request, send_file
from flask_socketio import SocketIO

import extractor
import store
from fields import PROPERTY_FIELDS, PARCEL_FIELDS, AGENCY_FIELDS, INSTALLMENT_FIELDS, normalize
from ui import HTML_TEMPLATE

SPEECH_KEY = os.environ.get("AZURE_SPEECH_KEY", "")   # checked when recording starts
SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "centralindia")
SPEECH_LANGUAGE = os.environ.get("SPEECH_LANGUAGE", "en-US")
_MODEL = os.environ.get("MODEL_DEPLOYMENT") or os.environ["AZURE_ANTHROPIC_DEPLOYMENT_NAME"]
LIVE_DEPLOYMENT = os.environ.get("LLM_LIVE_DEPLOYMENT", _MODEL)     # set a smaller model here to cut cost
FINAL_DEPLOYMENT = os.environ.get("LLM_FINAL_DEPLOYMENT", _MODEL)

EXTRACT_DEBOUNCE_SEC = 2.0    # wait for the speaker to finish before calling the LLM
LIVE_CONTEXT_TURNS = 12       # live extraction sees only the last N turns plus the current state
MAX_RECOGNIZER_RESTARTS = 5
CHUNK = 1024
ECHO_WINDOW_SEC = 2.5         # drop near-duplicate text heard on the other channel (mic bleed)
ECHO_SIMILARITY = 0.72

app = Flask(__name__)
socketio = SocketIO(app, async_mode="threading")   # same-origin only: no wildcard CORS


# ---------------------------------------------------------------- per-browser-session state
class Session:
    def __init__(self, sid):
        self.sid = sid
        self.lock = threading.Lock()
        self.active = False
        self.recognizers = []
        self.warned = set()
        self.dirty = threading.Event()
        self.reset()

    def reset(self):
        with self.lock:
            self.call_id = uuid.uuid4().hex
            self.log = []
            self.state = extractor.new_state()
            self.locked = set()    # (item_id, field_key) edited by the user: never overwritten
            self.deleted = set()   # signatures of items the user deleted: never re-added

    def push_state(self):
        with self.lock:
            snap = extractor.public_state(self.state)
        socketio.emit("state_update", snap, to=self.sid)

    def transcript(self):
        with self.lock:
            return "\n".join(f"[{m['time']}] {m['speaker']}: {m['text']}" for m in self.log)


SESSIONS = {}


def sess():
    return SESSIONS.setdefault(request.sid, Session(request.sid))


# ---------------------------------------------------------------- web
@app.route("/")
def index():
    return Response(HTML_TEMPLATE, mimetype="text/html")


@app.route("/export.xlsx")
def export_xlsx():
    return send_file(store.export_xlsx(), as_attachment=True, download_name="calls.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export_agencies.csv")
def export_agencies_csv():
    return send_file(os.path.abspath(store.write_agencies_csv()), as_attachment=True,
                     download_name="agencies.csv", mimetype="text/csv")


@socketio.on("connect")
def on_connect():
    s = sess()
    socketio.emit("config", {"property_fields": PROPERTY_FIELDS, "parcel_fields": PARCEL_FIELDS,
                             "agency_fields": AGENCY_FIELDS, "installment_fields": INSTALLMENT_FIELDS}, to=s.sid)
    s.push_state()


@socketio.on("disconnect")
def on_disconnect():
    s = SESSIONS.pop(request.sid, None)
    if s:
        stop_audio(s)


# ---------------------------------------------------------------- form edits from the UI
def _find(items, item_id):
    return next((i for i in items if i["id"] == item_id), None)


@socketio.on("field_edit")
def field_edit(d):
    s = sess()
    with s.lock:
        st = s.state
        if d["scope"] == "property":
            target, item_id = st["property"], "property"
        elif d["scope"] == "parcel":
            target = _find(st["parcels"], d["id"])
            item_id = d["id"]
        elif d["scope"] == "agency":
            target = next((a for p in st["parcels"] for a in p["agencies"] if a["id"] == d["id"]), None)
            item_id = d["id"]
        else:
            target = next((i for p in st["parcels"] for a in p["agencies"] for i in a["installments"]
                           if i["id"] == d["id"]), None)
            item_id = d["id"]
        if target is not None and d["key"] in target:
            value = d["value"]
            if d.get("final"):          # field left: clean the typed value like model values
                value = normalize(extractor._TYPES.get(d["key"], "string"), value)
            target[d["key"]] = value
            s.locked.add((item_id, d["key"]))
    s.push_state()


@socketio.on("add_parcel")
def add_parcel():
    s = sess()
    with s.lock:
        s.state["parcels"].append(extractor.new_parcel())
    s.push_state()


@socketio.on("add_agency")
def add_agency(d):
    s = sess()
    with s.lock:
        p = _find(s.state["parcels"], d["parcel_id"])
        if p:
            p["agencies"].append(extractor.new_agency())
    s.push_state()


@socketio.on("add_installment")
def add_installment(d):
    s = sess()
    with s.lock:
        a = next((a for p in s.state["parcels"] for a in p["agencies"] if a["id"] == d["agency_id"]), None)
        if a:
            a["installments"].append(extractor.new_installment())
    s.push_state()


@socketio.on("delete_parcel")
def delete_parcel(d):
    s = sess()
    with s.lock:
        p = _find(s.state["parcels"], d["id"])
        if p:
            pn = extractor._key(p["parcel_number"])
            if pn:
                s.deleted.add(("parcel", pn))
            s.state["parcels"].remove(p)
    s.push_state()


@socketio.on("delete_agency")
def delete_agency(d):
    s = sess()
    with s.lock:
        for p in s.state["parcels"]:
            a = _find(p["agencies"], d["id"])
            if a:
                if extractor._key(a["agency_name"]):
                    s.deleted.add(extractor.agency_signature(p["id"], a))
                p["agencies"].remove(a)
                break
    s.push_state()


@socketio.on("delete_installment")
def delete_installment(d):
    s = sess()
    with s.lock:
        for p in s.state["parcels"]:
            for a in p["agencies"]:
                i = _find(a["installments"], d["id"])
                if i:
                    s.deleted.add(extractor.installment_signature(a["id"], i))
                    a["installments"].remove(i)
                    break
    s.push_state()


@socketio.on("save_call")
def save_call():
    s = sess()
    with s.lock:
        snap = extractor.public_state(s.state)
    store.save_call(s.call_id, snap, s.transcript())
    socketio.emit("saved", snap, to=s.sid)


@socketio.on("clear_conversation")
def clear_conversation():
    s = sess()
    s.reset()
    s.push_state()


# ---------------------------------------------------------------- extraction
def run_extraction(s, final=False):
    with s.lock:
        turns = s.log if final else s.log[-LIVE_CONTEXT_TURNS:]
        text = "\n".join(f"{m['speaker']}: {m['text']}" for m in turns)
        snap = extractor.public_state(s.state)
    if not text:
        return
    try:
        result = extractor.extract(snap, text, FINAL_DEPLOYMENT if final else LIVE_DEPLOYMENT)
    except Exception as e:
        print("extraction failed:", e)
        return
    with s.lock:
        changed = extractor.merge(s.state, result, s.locked, s.deleted)
    if changed or final:
        s.push_state()


def extract_worker(s):
    """Runs extraction shortly after new speech is finalised, not on a fixed timer."""
    while s.active:
        if not s.dirty.wait(timeout=1):
            continue
        s.dirty.clear()
        time.sleep(EXTRACT_DEBOUNCE_SEC)
        if s.active:
            run_extraction(s)


@socketio.on("start_recording")
def start_recording():
    s = sess()
    if s.active:
        return
    if not SPEECH_KEY:
        socketio.emit("error_message", "AZURE_SPEECH_KEY is missing in .env. Add it (and AZURE_SPEECH_REGION) and restart.", to=s.sid)
        return
    s.reset()
    s.active = True
    s.recognizers = []
    s.warned = set()
    socketio.emit("recording_started", to=s.sid)
    s.push_state()
    for target in (extract_worker, mic_worker, speaker_worker):
        threading.Thread(target=target, args=(s,), daemon=True).start()


def stop_audio(s):
    s.active = False
    for rec in list(s.recognizers):
        try:
            rec.stop_continuous_recognition()
        except Exception:
            pass


@socketio.on("stop_recording")
def stop_recording():
    s = sess()
    stop_audio(s)
    time.sleep(0.3)
    run_extraction(s, final=True)          # one last, full-transcript pass
    with s.lock:
        snap = extractor.public_state(s.state)
    store.save_call(s.call_id, snap, s.transcript())
    socketio.emit("recording_stopped", to=s.sid)
    socketio.emit("saved", snap, to=s.sid)


# ---------------------------------------------------------------- speech recognition
def _is_echo(s, speaker, text):
    now = time.time()
    with s.lock:
        for m in reversed(s.log[-6:]):
            if m["speaker"] != speaker and now - m["_ts"] <= ECHO_WINDOW_SEC:
                if SequenceMatcher(None, m["text"].lower(), text.lower()).ratio() >= ECHO_SIMILARITY:
                    return True
    return False


def _speech_config():
    cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SPEECH_REGION)
    cfg.speech_recognition_language = SPEECH_LANGUAGE
    cfg.set_property(speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, "1200")
    cfg.set_property(speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "15000")
    return cfg


def warn(s, label, err):
    """Show an audio/speech problem on the page (once per source per call) and in the terminal."""
    text = str(err)
    who = "Microphone (Caller-1)" if label == "Caller-1" else "System audio (Caller-2)"
    if "MIC_NOT_AVAILABLE" in text or "0xe" in text:
        msg = (f"{who}: no microphone found. Connect a mic and click Stop then Start. "
               "On Remote Desktop, enable 'Record from this computer' under Local Resources > Remote audio.")
    else:
        msg = f"{who}: {text}"
    print(f"[{label}] {msg}")
    if label not in s.warned:
        s.warned.add(label)
        socketio.emit("warning_message", msg, to=s.sid)


def run_recognizer(s, build, label):
    """Continuous recognition that rebuilds itself if Azure drops the session mid-call."""
    restarts = 0
    while s.active and restarts <= MAX_RECOGNIZER_RESTARTS:
        ended = threading.Event()
        try:
            rec = build()
        except Exception as e:
            warn(s, label, e)
            return
        s.recognizers.append(rec)

        def on_final(evt, label=label):
            if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
                return
            text = evt.result.text.strip()
            if not text or _is_echo(s, label, text):
                return
            ts = datetime.now().strftime("%H:%M:%S")
            with s.lock:
                s.log.append({"speaker": label, "text": text, "time": ts, "_ts": time.time()})
            socketio.emit("new_message", {"speaker": label, "text": text, "timestamp": ts}, to=s.sid)
            s.dirty.set()

        def on_partial(evt, label=label):
            if evt.result.text:
                socketio.emit("partial_result", {"speaker": label, "text": evt.result.text}, to=s.sid)

        def on_end(evt, label=label):
            print(f"[{label}] session ended: {evt}")
            ended.set()

        rec.recognized.connect(on_final)
        rec.recognizing.connect(on_partial)
        def on_cancel(evt, label=label):
            details = getattr(evt, "cancellation_details", None)
            if details is not None and details.reason == speechsdk.CancellationReason.Error:
                warn(s, label, details.error_details)
            on_end(evt)

        rec.canceled.connect(on_cancel)
        rec.session_stopped.connect(on_end)
        rec.start_continuous_recognition()
        while s.active and not ended.is_set():
            time.sleep(0.2)
        try:
            rec.stop_continuous_recognition()
        except Exception:
            pass
        if rec in s.recognizers:
            s.recognizers.remove(rec)
        if not s.active:
            return
        restarts += 1
        print(f"[{label}] restarting recognizer ({restarts}/{MAX_RECOGNIZER_RESTARTS})")
        time.sleep(1.0)


def mic_worker(s):
    def build():
        audio = speechsdk.audio.AudioConfig(use_default_microphone=True)
        return speechsdk.SpeechRecognizer(_speech_config(), audio)

    run_recognizer(s, build, "Caller-1")


def _loopback_device(p):
    """WASAPI loopback of the default speakers = what the remote participant says."""
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    speakers = p.get_device_info_by_index(api["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice", False):
        return speakers
    for dev in p.get_loopback_device_info_generator():
        if speakers["name"] in dev["name"]:
            return dev
    raise RuntimeError("No WASAPI loopback device matches the default speakers")


def speaker_worker(s):
    try:
        p = pyaudio.PyAudio()
        dev = _loopback_device(p)
    except Exception as e:
        warn(s, "Caller-2", f"not captured ({e})")
        return
    rate, channels = int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"]))
    push = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(samples_per_second=rate, channels=channels))

    def build():
        return speechsdk.SpeechRecognizer(_speech_config(), speechsdk.audio.AudioConfig(stream=push))

    threading.Thread(target=run_recognizer, args=(s, build, "Caller-2"), daemon=True).start()
    try:
        stream = p.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                        input_device_index=dev["index"], frames_per_buffer=CHUNK)
    except Exception as e:
        warn(s, "Caller-2", f"could not open the audio stream ({e})")
        p.terminate()
        return
    try:
        while s.active:
            push.write(stream.read(CHUNK, exception_on_overflow=False))
    except Exception as e:
        print("Loopback read error:", e)
    finally:
        for closer in (push.close, stream.close, p.terminate):
            try:
                closer()
            except Exception:
                pass


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000")
    socketio.run(app, host="127.0.0.1", port=5000, debug=False, allow_unsafe_werkzeug=True)
