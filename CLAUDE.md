# Outpost — working notes

Offline ejudge: one laptop judges, contestants join its Wi-Fi. `README.md` is the
user guide, `docs/DESIGN.md` holds the architecture, decisions and roadmap. Add new
decisions there.

- Nothing may load from the internet at contest time. Vendor any frontend asset under
  `outpost/static/vendor/` (KaTeX is there already).
- Contest rules mirror electicode: scoring modes, penalty and freeze, divisions,
  verdict names, language ids `cpp`/`java21`/`py12`. Check
  `../Platform-Scraper/docs/ADMIN_API.md` before changing them.
- Answers are generated from the Polygon **main** solution only.
- Tests: `sudo -E python -m pytest`. Root is needed for the uid and network
  isolation tests, and g++ plus a JDK must be installed.
