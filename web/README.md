# Meta Nuke web front end

Upload images / PDFs / SVGs, see what metadata they carry, strip it, download a zip.
Runs the real `metanuke` library from this checkout, so the web path and the CLI path
can't drift apart.

Only reachable over the tailnet: the app binds `127.0.0.1:8790` and `tailscale serve`
publishes it at https://designer-box.tail8ea96b.ts.net/ — nothing is exposed to the LAN
or the internet.

## Files

| path | what |
|---|---|
| `app.py` | Flask app: `/`, `/healthz`, `/api/preview`, `/api/nuke` |
| `templates/index.html` | single-page UI, no external assets |
| `requirements.txt` | web-only deps (flask, waitress, pikepdf) |
| `/etc/systemd/system/metanuke-web.service` | runs it, restarts on failure |

## Deploy / update

```bash
cd /opt/design/tools/meta-nuke
.venv/bin/pip install -r web/requirements.txt
.venv/bin/pip install -e . --no-deps --no-build-isolation   # the tool itself
systemctl restart metanuke-web
tailscale serve --bg --https=443 http://127.0.0.1:8790      # once; persists in tailscaled state
```

`tailscale serve status` shows the mapping; `tailscale serve --https=443 off` removes it.

## Behaviour worth knowing

- Every request stages into `/var/lib/metanuke-web/job-*` (systemd `StateDirectory`) and
  deletes it in a `finally`. Directories older than 6 hours are swept at startup.
- Limits: 60 files and 300 MB per upload (`METANUKE_MAX_FILES`, `METANUKE_MAX_UPLOAD_MB`).
- The zip always contains `metanuke-report.json`: per-file before flags, byte counts,
  residual metadata after, noise level, hash-name mode, timestamp.
- `noise 0` is the default: lossless, metadata still fully stripped. Levels 1-10 add
  per-pixel noise and a second JPEG pass, which grows files (a 15 KB test JPEG came back
  at 44 KB at noise 5).
- PDFs get a pikepdf pass after nuking: PyMuPDF (which `MetaNuke._nuke_pdf` uses) stamps
  `/Producer (MuPDF x.y.z)` into the catalog and writes a `% Written by MuPDF` header
  comment on every save, so without the scrub a "nuked" PDF still names its toolchain.
  The CLI does not do this — it is web-only, in `_scrub_pdf()`.
- Verification is by re-inspection, not by trusting the tool's own summary: each output
  file is re-described (EXIF tags, ICC, XMP, PNG text chunks, `/Producer`, MuPDF/Adobe/AI
  markers) and anything still found is reported as `residual` and flagged red in the UI.

## Testing without a browser

```bash
curl -sS https://designer-box.tail8ea96b.ts.net/healthz
curl -sS -o out.zip -F "files=@photo.jpg" -F "noise=0" -F "rename=0" \
     https://designer-box.tail8ea96b.ts.net/api/nuke
unzip -o out.zip -d out && cat out/metanuke-report.json
```
