/* ============================================================
   Shared behaviour for every page. Small on purpose.
   ============================================================ */

/* The demo address lives here alone. Change this one line if the
   demo ever moves; every page picks it up. */
const DEMO_URL = "https://demo.hecinbox.com";

document.querySelectorAll("a.demo-link").forEach(a => { a.href = DEMO_URL; });

/* current year in the footer */
document.querySelectorAll(".yr").forEach(el => { el.textContent = new Date().getFullYear(); });

/* mobile menu */
(function menu(){
  const b = document.querySelector(".burger");
  const t = document.querySelector(".tabs");
  if (!b || !t) return;
  b.addEventListener("click", () => t.classList.toggle("open"));
  t.addEventListener("click", e => { if (e.target.tagName === "A") t.classList.remove("open"); });
})();

/* highlight the tab for the page we are on, driven by <body data-page> */
(function activeTab(){
  const page = document.body.dataset.page;
  if (!page) return;
  const link = document.querySelector(`.tabs a[data-tab="${page}"]`);
  if (link) link.classList.add("on");
})();

/* FAQ accordion, only on pages that have one */
(function faq(){
  const items = document.querySelectorAll(".faq-item");
  if (!items.length) return;
  items.forEach(item => {
    const q = item.querySelector(".faq-q");
    const a = item.querySelector(".faq-a");
    q.addEventListener("click", () => {
      const open = item.classList.contains("open");
      items.forEach(x => { x.classList.remove("open"); x.querySelector(".faq-a").style.maxHeight = null; });
      if (!open) { item.classList.add("open"); a.style.maxHeight = a.scrollHeight + "px"; }
    });
  });
})();

/* install command tabs plus copy, only on the deploy page */
(function deploy(){
  const tabs = document.getElementById("codeTabs");
  const out = document.getElementById("codeOut");
  const btn = document.getElementById("copyBtn");
  if (!tabs || !out) return;

  const IMG = "ehsankahrizi1991/hecinbox:latest";
  const CODE = {
    mac: `<span class="c"># a folder for finished runs</span>
mkdir -p ~/HEC-RAS-Outputs

docker run -d --platform linux/amd64 <span class="k">-p</span> 8501:8501 \\
  <span class="k">--cpus</span>=4 \\
  <span class="k">-v</span> ~/:/host:ro \\
  <span class="k">-v</span> ~/HEC-RAS-Outputs:/host_out \\
  <span class="s">${IMG}</span>

<span class="c"># then open http://localhost:8501</span>`,
    win: `<span class="c"># PowerShell</span>
mkdir "$HOME\\HEC-RAS-Outputs"

docker run -d --platform linux/amd64 <span class="k">-p</span> 8501:8501 \`
  <span class="k">--cpus</span>=4 \`
  <span class="k">-v</span> $HOME:/host:ro \`
  <span class="k">-v</span> $HOME\\HEC-RAS-Outputs:/host_out \`
  <span class="s">${IMG}</span>

<span class="c"># then open http://localhost:8501</span>`,
    cloud: `<span class="c"># model read from a bucket, results written back</span>
docker run -d --platform linux/amd64 <span class="k">-p</span> 8501:8501 \\
  <span class="k">--cpus</span>=8 \\
  <span class="k">-e</span> AWS_DEFAULT_REGION=us-east-2 \\
  <span class="k">-e</span> AWS_ACCESS_KEY_ID=<span class="s">&lt;key&gt;</span> \\
  <span class="k">-e</span> AWS_SECRET_ACCESS_KEY=<span class="s">&lt;secret&gt;</span> \\
  <span class="s">${IMG}</span>

<span class="c"># on EC2 or ECS attach an IAM role and pass no keys</span>`
  };
  const RAW = {
    mac: `mkdir -p ~/HEC-RAS-Outputs\n\ndocker run -d --platform linux/amd64 -p 8501:8501 \\\n  --cpus=4 \\\n  -v ~/:/host:ro \\\n  -v ~/HEC-RAS-Outputs:/host_out \\\n  ${IMG}`,
    win: `mkdir "$HOME\\HEC-RAS-Outputs"\n\ndocker run -d --platform linux/amd64 -p 8501:8501 \`\n  --cpus=4 \`\n  -v $HOME:/host:ro \`\n  -v $HOME\\HEC-RAS-Outputs:/host_out \`\n  ${IMG}`,
    cloud: `docker run -d --platform linux/amd64 -p 8501:8501 \\\n  --cpus=8 \\\n  -e AWS_DEFAULT_REGION=us-east-2 \\\n  -e AWS_ACCESS_KEY_ID=<key> \\\n  -e AWS_SECRET_ACCESS_KEY=<secret> \\\n  ${IMG}`
  };

  let key = "mac";
  out.innerHTML = CODE[key];
  tabs.addEventListener("click", e => {
    const b = e.target.closest(".code-tab");
    if (!b) return;
    key = b.dataset.code;
    tabs.querySelectorAll(".code-tab").forEach(t => t.classList.toggle("on", t === b));
    out.innerHTML = CODE[key];
  });
  if (btn) btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(RAW[key]);
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = "Copy"; }, 1600);
    } catch { btn.textContent = "Press Cmd+C"; }
  });
})();

/* Homepage screenshot. The file may not be there yet, so swap in the
   built-in illustration rather than leaving a broken image. */
(function heroShot(){
  const img = document.getElementById("heroShot");
  const fb = document.getElementById("heroShotFallback");
  if (!img || !fb) return;

  function drawFallback(){
    const g = document.getElementById("fbCells");
    if (!g) return;
    const ramp = ["#e0f2fe","#bae6fd","#7dd3fc","#4b93c9","#3c78af","#1e4470"];
    let out = "";
    for (let r = 0; r < 19; r++){
      for (let c = 0; c < 50; c++){
        const x = c * 20, y = r * 20;
        const d = Math.abs((y + 16) - (330 - x * 0.30)) / 70;
        const v = 1 - d + Math.sin(c * 0.5) * 0.16 + Math.cos(r * 0.9) * 0.12;
        if (v <= 0.08) continue;
        out += `<rect x="${x}" y="${y}" width="19" height="19" fill="${ramp[Math.min(5, Math.floor(v * 6))]}" opacity="${(0.5 + v * 0.5).toFixed(2)}"/>`;
      }
    }
    g.innerHTML = out;
  }
  function swap(){ img.remove(); fb.hidden = false; drawFallback(); }

  img.addEventListener("error", swap);
  if (img.complete && img.naturalWidth === 0) swap();
})();
