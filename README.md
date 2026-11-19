# REED mesh status

Read-only status page for the REED MeshCore repeaters: https://andrewlewisreed.github.io/

- `index.html` - static page, reads `data.json` in the browser.
- `scripts/update_data.py` - fetches public data from [CoreScope](https://corescope.wcmesh.com/) and computes the REED score (same formula as the meshcore-bot).
- `.github/workflows/update.yml` - runs the script every 30 minutes and commits `data.json` when it changes.

Only public CoreScope data and public keys are used. No locations, no secrets.
