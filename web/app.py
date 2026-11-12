"""Meta Nuke web front end.

Upload images/PDFs/SVGs, inspect what they carry, strip it, download a zip.
Runs the real `metanuke` library from this checkout, so the web path and the
CLI path cannot drift apart.

Bound to loopback and published on the tailnet by `tailscale serve`.
"""

import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from PIL import ExifTags, Image

from metanuke import __version__
from metanuke.core import MetaNuke

MAX_FILES = int(os.environ.get("METANUKE_MAX_FILES", "60"))
MAX_UPLOAD_MB = int(os.environ.get("METANUKE_MAX_UPLOAD_MB", "300"))
WORKROOT = Path(os.environ.get("METANUKE_WORKROOT", "/var/tmp/metanuke-web"))
PORT = int(os.environ.get("METANUKE_PORT", "8790"))

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

AI_MARKERS = (b"c2pa", b"trainedAlgorithmicMedia", b"digitalSourceType",
              b"gpt-image", b"DALL-E", b"generative")
CAMERA_TAGS = {
    271: "camera make", 272: "camera model", 274: "orientation", 305: "software",
    306: "timestamp", 315: "author", 316: "host computer", 33432: "copyright",
    42036: "lens model", 36867: "date taken", 36868: "date digitised",
}
GPS_IFD = 34853


def _short(value, limit=64):
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[: limit - 3] + "..." if len(text) > limit else text


def _header_list(values):
    """Comma-joined, ASCII-safe header value for the raw download's report."""
    if not values:
        return ""
    text = ",".join(str(v).replace(",", " ") for v in values)
    return text.encode("ascii", "replace").decode("ascii")[:900]


def _scan_bytes(path):
    """Return labels for metadata blobs that live outside EXIF."""
    try:
        raw = Path(path).read_bytes()[: 4 * 1024 * 1024]
    except OSError:
        return []
    hits = []
    if b"<x:xmpmeta" in raw or b"http://ns.adobe.com/xap" in raw:
        hits.append("XMP packet")
    if b"Photoshop" in raw or b"Adobe" in raw:
        hits.append("Adobe marker")
    if b"/Producer" in raw or b"/Creator" in raw:
        hits.append("document properties")
    if b"MuPDF" in raw:
        hits.append("PDF tool fingerprint")
    if any(m in raw for m in AI_MARKERS):
        hits.append("AI/C2PA provenance")
    if b"icc_profile" in raw.lower() or b"ICC_PROFILE" in raw:
        hits.append("ICC profile")
    return hits


def _describe_exif(path):
    findings, flags = [], []
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            for tag_id, value in exif.items():
                label = CAMERA_TAGS.get(tag_id) or ExifTags.TAGS.get(tag_id, str(tag_id))
                if isinstance(value, bytes):
                    continue
                findings.append({"tag": label, "value": _short(value)})
            if exif.get(GPS_IFD):
                findings.append({"tag": "GPS", "value": "location block present"})
                flags.append("GPS location")
            try:
                exif_ifd = exif.get_ifd(0x8769)
                for tag_id, value in exif_ifd.items():
                    label = CAMERA_TAGS.get(tag_id) or ExifTags.TAGS.get(tag_id, str(tag_id))
                    if isinstance(value, bytes):
                        continue
                    findings.append({"tag": label, "value": _short(value)})
            except Exception:
                pass
            # PNG text chunks / iTXt
            for key, value in (im.info or {}).items():
                if key in ("icc_profile",):
                    findings.append({"tag": "ICC profile", "value": "embedded"})
                    flags.append("ICC profile")
                    continue
                if key in ("exif", "dpi", "jfif", "jfif_version", "jfif_unit",
                           "jfif_density", "transparency", "gamma", "profile"):
                    continue
                if isinstance(value, (bytes, str)):
                    findings.append({"tag": f"text:{key}", "value": _short(value)})
    except Exception as exc:
        findings.append({"tag": "unreadable", "value": _short(exc)})
    return findings, flags


def _describe_pdf(path):
    findings = []
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return findings, []
    try:
        doc = pymupdf.open(path)
        for key, value in (doc.metadata or {}).items():
            if value and key != "format":   # 'format' is the PDF version, not metadata
                findings.append({"tag": f"pdf:{key}", "value": _short(value)})
        doc.close()
    except Exception as exc:
        findings.append({"tag": "unreadable", "value": _short(exc)})
    return findings, []


def _describe_svg(path):
    findings = []
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return findings, []
    for tag in ("metadata", "desc", "title"):
        for match in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S | re.I)[:3]:
            findings.append({"tag": f"svg:{tag}", "value": _short(match)})
    return findings, []


def describe(path):
    path = Path(path)
    suffix = path.suffix.lower()
    findings, flags = [], []
    if suffix in (".pdf",):
        findings, flags = _describe_pdf(path)
    elif suffix == ".svg":
        findings, flags = _describe_svg(path)
    else:
        findings, flags = _describe_exif(path)
    blob_hits = _scan_bytes(path)
    flags = flags + blob_hits
    if any(f["tag"] in ("camera make", "camera model", "lens model") for f in findings):
        flags.append("camera identity")
    if any(f["tag"] == "software" for f in findings):
        flags.append("software fingerprint")
    if any(f["tag"] in ("author", "copyright") for f in findings):
        flags.append("authorship")
    tags = [f["tag"] for f in findings]
    if any(t.startswith("pdf:") for t in tags):
        flags.append("document metadata")
    if any(t.startswith("svg:") for t in tags):
        flags.append("svg annotations")
    if any(t.startswith("text:") for t in tags):
        flags.append("embedded text chunks")
    if any(t.endswith(("author", "artist")) for t in tags):
        flags.append("authorship")
    if any(t.endswith(("software", "creator", "producer")) for t in tags):
        flags.append("software fingerprint")
    if any(t.endswith(("title", "subject", "keywords")) for t in tags):
        flags.append("document title")
    # dedupe, keep order
    seen, clean = set(), []
    for flag in flags:
        if flag not in seen:
            seen.add(flag)
            clean.append(flag)
    return {
        "name": path.name,
        "format": suffix.lstrip(".") or "?",
        "bytes": path.stat().st_size,
        "findings": findings[:40],
        "findings_total": len(findings),
        "flags": clean,
    }


def _safe_name(name, taken):
    base = os.path.basename(name or "upload").strip() or "upload"
    base = base.replace("\x00", "")
    if base.startswith("."):
        base = "_" + base.lstrip(".")
    stem, suffix = os.path.splitext(base)
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{stem}-{n}{suffix}"
    taken.add(candidate)
    return candidate


def _scrub_pdf(path):
    """PyMuPDF stamps /Producer (MuPDF x.y.z) into the catalog and writes a
    '% Written by MuPDF' header comment on every save, so a nuked PDF still
    names its tools. Remove both; page content and structure stay untouched.
    """
    try:
        import pikepdf
    except ImportError:
        return False
    tmp = path.with_name(path.name + ".scrub")
    try:
        with pikepdf.open(path) as pdf:
            for obj in (pdf.Root, pdf.trailer):
                if "/Info" in obj:
                    del obj["/Info"]
            pdf.save(tmp)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _report_entry():
    """Zip entry for the report with normal 0644 permissions."""
    info = zipfile.ZipInfo("metanuke-report.json", time.localtime()[:6])
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _sweep_old_jobs(max_age_hours=6):
    """Remove staging dirs a crashed or killed request left behind."""
    cutoff = time.time() - max_age_hours * 3600
    for entry in WORKROOT.glob("job-*"):
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


@app.route("/")
def index():
    return render_template("index.html", version=__version__,
                           max_files=MAX_FILES, max_mb=MAX_UPLOAD_MB)


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, version=__version__, formats=sorted(MetaNuke.SUPPORTED_FORMATS))


@app.post("/api/preview")
def preview():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="no files"), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"too many files (max {MAX_FILES})"), 400
    WORKROOT.mkdir(parents=True, exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="job-", dir=WORKROOT))
    try:
        taken, reports = set(), []
        for upload in files:
            name = _safe_name(upload.filename, taken)
            target = job / name
            upload.save(target)
            report = describe(target)
            report["name"] = upload.filename or name
            reports.append(report)
        return jsonify(version=__version__, files=reports)
    finally:
        shutil.rmtree(job, ignore_errors=True)


@app.post("/api/nuke")
def nuke():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="no files"), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"too many files (max {MAX_FILES})"), 400
    try:
        noise = max(0, min(10, int(request.form.get("noise", "0"))))
    except ValueError:
        return jsonify(error="noise must be 0-10"), 400
    rename = request.form.get("rename") == "1"
    output_mode = request.form.get("output", "zip")
    if output_mode not in ("zip", "raw"):
        return jsonify(error="output must be zip or raw"), 400

    WORKROOT.mkdir(parents=True, exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="job-", dir=WORKROOT))
    try:
        (job / "in").mkdir()
        (job / "out").mkdir()
        taken, results = set(), []
        for upload in files:
            name = _safe_name(upload.filename, taken)
            src = job / "in" / name
            upload.save(src)
            suffix = src.suffix.lower()
            if suffix not in MetaNuke.SUPPORTED_FORMATS:
                results.append({"name": upload.filename or name, "ok": False,
                                "message": f"unsupported format: {suffix or 'no extension'}"})
                continue
            before = describe(src)
            # MetaNuke treats output_path as a FILE normally, but as a DIRECTORY
            # when rename=True (it appends the content hash itself) — passing the
            # file path there makes it write <out>/<name>/<hash>.jpg.
            dest = (job / "out") if rename else (job / "out" / name)
            ok, message = MetaNuke.nuke_image(
                str(src), noise_level=noise, output_path=str(dest), rename=rename)
            entry = {
                "name": upload.filename or name,
                "ok": bool(ok),
                "message": _short(message, 160),
                "before": {"flags": before["flags"], "findings_total": before["findings_total"],
                           "bytes": before["bytes"]},
                "noise": noise,
            }
            if ok and dest.exists():
                if dest.suffix.lower() == ".pdf":
                    entry["pdf_scrubbed"] = _scrub_pdf(dest)
                after = describe(dest)
                entry["after"] = {"flags": after["flags"],
                                  "findings_total": after["findings_total"],
                                  "bytes": after["bytes"], "file": dest.name}
                entry["residual"] = after["flags"]
            results.append(entry)

        produced = sorted(p for p in (job / "out").iterdir() if p.is_file())
        for path in produced:
            try:
                os.chmod(path, 0o644)   # keep downloaded files readable, not 0600
            except OSError:
                pass
        report = {
            "tool": f"meta-nuke {__version__}",
            "noise_level": noise,
            "hash_names": rename,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": results,
        }

        if output_mode == "raw":
            # One nuked file straight to the browser — no zip, no report file.
            # Everything the UI needs rides in headers; the job dir dies in `finally`.
            if len(results) != 1:
                return jsonify(error="Save file takes exactly one file; use the zip for a batch"), 400
            entry = results[0]
            if not produced:
                return jsonify(error=_short(entry.get("message") or "purge failed", 160)), 400
            path = produced[0]
            data = path.read_bytes()
            response = send_file(
                io.BytesIO(data),
                mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                as_attachment=True,
                download_name=path.name,
            )
            before = entry.get("before") or {}
            response.headers["X-Metanuke-Ok"] = "1"
            response.headers["X-Metanuke-Failed"] = "0"
            response.headers["X-Metanuke-Bytes"] = str(len(data))
            response.headers["X-Metanuke-Before-Bytes"] = str(before.get("bytes") or 0)
            response.headers["X-Metanuke-Findings"] = str(before.get("findings_total") or 0)
            response.headers["X-Metanuke-Flags"] = _header_list(before.get("flags"))
            response.headers["X-Metanuke-Residual"] = _header_list(entry.get("residual"))
            return response

        stamp = time.strftime("%Y%m%d-%H%M%S")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in produced:
                bundle.write(path, arcname=path.name)
            bundle.writestr(_report_entry(), json.dumps(report, indent=2))
        buffer.seek(0)
        response = send_file(buffer, mimetype="application/zip",
                             as_attachment=True,
                             download_name=f"metanuke-{stamp}.zip")
        response.headers["X-Metanuke-Ok"] = str(sum(1 for r in results if r["ok"]))
        response.headers["X-Metanuke-Failed"] = str(sum(1 for r in results if not r["ok"]))
        response.headers["X-Metanuke-Report"] = "metanuke-report.json"
        return response
    finally:
        shutil.rmtree(job, ignore_errors=True)


@app.errorhandler(413)
def too_large(_err):
    return jsonify(error=f"upload larger than {MAX_UPLOAD_MB} MB"), 413


if __name__ == "__main__":
    _sweep_old_jobs()
    try:
        from waitress import serve
    except ImportError:
        app.logger.warning("waitress missing; falling back to the Flask dev server")
        app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
    else:
        serve(app, host="127.0.0.1", port=PORT, threads=4)
