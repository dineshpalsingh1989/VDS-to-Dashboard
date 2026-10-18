"""Bundle the real dashboard's styles, icons, and Plotly for offline demo views."""
from pathlib import Path
import json
import re
import shutil
import urllib.request

import dash_bootstrap_components as dbc
from plotly.offline import get_plotlyjs

root = Path(__file__).resolve().parent.parent
vendor = root / "demo" / "vendor"
vendor.mkdir(parents=True, exist_ok=True)
manifest_file = vendor / "sources.json"
sources = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}

def download(url, relative):
    target = vendor / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and sources.get(relative) == url:
        return target.read_text(encoding="utf-8") if relative.endswith(".css") else None
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read()
    target.write_bytes(content)
    sources[relative] = url
    return content.decode() if relative.endswith(".css") else None

download(dbc.themes.BOOTSTRAP, "bootstrap.css")
font_version = re.search(r"/releases/v([^/]+)/", dbc.icons.FONT_AWESOME).group(1)
font_base = f"https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@{font_version}"
css = download(font_base + "/css/all.min.css", "css/fontawesome.css")
for filename in sorted(set(re.findall(r"\.\./webfonts/([^)'\"]+)", css))):
    download(font_base + "/webfonts/" + filename, "webfonts/" + filename)
download("https://raw.githubusercontent.com/FortAwesome/Font-Awesome/6.7.2/LICENSE.txt", "fontawesome-LICENSE.txt")
(vendor / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
shutil.copyfile(root / "assets" / "dashboard.css", vendor / "dashboard.css")
manifest_file.write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
print("Bundled real dashboard CSS, Font Awesome, and Plotly.")
