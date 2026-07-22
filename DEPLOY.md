# Deploying hecinbox.com

Static site. No build step, no dependencies, no server. Files:

```
wrangler.jsonc          Cloudflare config: publish public/, serve 404.html
README.md, LICENSE      repository material, never served
DEPLOY.md               this file, never served
public/                 everything below here is the live site
  index.html            the whole site (inline CSS and JS)
  404.html              not-found page
  logo.png              full brand logo, also used as the social preview image
  logo-mark.png         logo art without the wordmark, used in the header and footer
  apple-touch-icon.png  home screen icon
  favicon.png           browser tab icon
  screenshots/          result maps shown in the Results gallery, see its README
  _headers              security headers applied at the edge
  robots.txt
  sitemap.xml
```

Only `public/` is uploaded. That is why the site files sit in a subfolder: it
keeps the repository's own documentation off the live domain.

The palette and typography are copied from the application itself, so the site
and the product look like one piece of software:

| Token | Value | Source |
|---|---|---|
| Page background | `#eef0f5` | `backgroundColor` in `.streamlit/config.toml` |
| Text | `#0f1e3a` | `textColor` |
| Primary | `#3c78af` | `primaryColor` |
| Accent wash | `#e1e6ee` | `secondaryBackgroundColor` |
| Body font | Inter | the app's `_THEME_RULES` |
| Code font | JetBrains Mono | the app's `_THEME_RULES` |

The two fonts are the only external request the page makes, loaded from Google
Fonts exactly as the application loads them. `_headers` allows those two hosts
in the content security policy and nothing else.

## Host: Cloudflare, connected to this repository

The domain is already registered at Cloudflare, so this is the path of least
resistance and the more secure of the free options.

| | Cloudflare | GitHub Pages |
|---|---|---|
| Cost | Free, unlimited bandwidth | Free, 100 GB/month soft limit |
| Custom domain on a Cloudflare domain | One click, DNS wired automatically | Manual A/AAAA/CNAME records |
| TLS | Free, automatic | Free, automatic |
| DDoS protection and WAF | Included | Not included |
| Custom response headers | Yes, via `_headers` | No |
| Private source repo | Allowed on the free plan | Needs GitHub Pro |
| Deploy previews per branch | Yes | No |

GitHub stays the source of truth and Cloudflare builds from it, so there is no
reason to choose between them.

### Steps

The site lives in `public/` in **github.com/Ehsankahrizi/HECinBOX**, and
`wrangler.jsonc` already tells Cloudflare that. Only the dashboard side is left.

1. Cloudflare dashboard, **Workers & Pages**, **Create**, **Import a repository**.
2. Authorize GitHub if asked, then pick `Ehsankahrizi/HECinBOX`, branch `main`.
3. On the setup screen:
   - **Project name:** `hecinbox`. It must be lowercase. The dashboard prefills
     it from the repository name, which is mixed case and gets rejected.
   - **Build command:** leave it empty. There is nothing to build.
   - **Deploy command:** leave the default, `npx wrangler deploy`.
   - Do not set a build output directory. `wrangler.jsonc` supplies it.
4. Deploy. You get a `hecinbox.<something>.workers.dev` URL in about a minute.
5. Attach the domain, see the section below.
6. In the zone under **SSL/TLS**, set the encryption mode to **Full (strict)**.

Every push to `main` redeploys. Rollback is one click in the deployment list.

### Why a Worker and not Pages

Cloudflare now steers new projects into Workers, and Workers static assets cover
everything this site needs: `_headers`, a real 404 page, unlimited free
bandwidth, and the same edge network. If your dashboard still offers the Pages
flow and you prefer it, it works too. Use build command empty and build output
directory `public`; `wrangler.jsonc` is simply ignored on that path.

## The custom domain

`hecinbox.com` is the single canonical address. `www` redirects to it rather
than serving a second copy, so search engines never see the same page at two
URLs.

State of the zone before any of this was set up: nameservers already on
Cloudflare, **no MX records** so no email to disturb, `www` had no records at
all, and the apex carried proxied A and AAAA records that answered nothing.
Attaching the Worker replaces those apex records, which is expected.

### Attach the apex

Worker project, **Settings**, **Domains & Routes**, **Add**, **Custom domain**,
enter `hecinbox.com`. Cloudflare warns that it will replace the existing DNS
record: accept. It writes the record and issues the certificate itself because
the zone is on the same account. Do **not** add `www` here, it is handled by a
redirect instead.

### Redirect www to the apex

A redirect rule only fires for a hostname that resolves through Cloudflare, so
`www` needs a record before the rule will do anything.

1. **DNS**, **Add record**: type `CNAME`, name `www`, target `hecinbox.com`,
   proxy status **Proxied** (orange cloud). The record is never actually
   fetched, it exists so Cloudflare terminates the request.
2. **Rules**, **Redirect Rules**, **Create rule**. Cloudflare ships a
   *Redirect from WWW to Root* template that fills this in correctly; use it if
   offered. Building it by hand:
   - If: Hostname equals `www.hecinbox.com`
   - Then: Dynamic redirect, expression
     `concat("https://hecinbox.com", http.request.uri.path)`
   - Status **301**, preserve query string **on**

Use the dynamic form rather than a static one. A static redirect sends every
visitor to the homepage, so a shared deep link would lose its path.

### After the domain serves

Turn the `workers.dev` URL off, in the same **Domains & Routes** screen. The
site is then reachable at one address only. Do this in the dashboard: pinning
route settings in `wrangler.jsonc` is what caused the error 1042 outage.

## The demo link needs HTTPS

`index.html` points the demo buttons at `DEMO_URL`, defined at the top of the
script block. It currently holds the plain HTTP ALB address:

```
http://bil6-hecinbox-alb-1394482369.us-east-2.elb.amazonaws.com
```

An HTTPS site linking to an HTTP address is not blocked, but the browser labels
the demo "Not secure" and some corporate networks strip it.

### Why not an ACM certificate on the ALB

That was the original plan and it cannot be done with the current credentials.
The IAM user `ekahrizi@crimson.ua.edu` carries `BIL6-HECRAS-Access`, which
grants `ec2`, `elasticloadbalancing`, `ecs`, `s3` and more but **no `acm:*`
actions at all**. Neither workaround is open either: there is no
`iam:CreatePolicyVersion` to grant it to yourself, and no
`iam:UploadServerCertificate`. An ALB accepts certificates only from ACM or
IAM, so the listener route is closed until an account administrator widens the
policy.

### What is done instead: Cloudflare terminates TLS

Cloudflare's Universal SSL already covers `*.hecinbox.com`, so a proxied
subdomain gets a valid certificate with nothing to request and nothing to
renew.

1. **DNS**, **Add record**: type `CNAME`, name `demo`, target
   `bil6-hecinbox-alb-1394482369.us-east-2.elb.amazonaws.com`, proxy status
   **Proxied** (orange cloud). Proxied is required: DNS only would expose the
   plain HTTP origin directly and give no certificate.
2. **This step is mandatory, the demo returns a 522 error without it.** The
   zone is set to Full (strict), which makes Cloudflare fetch the origin over
   HTTPS. The ALB listens on port 80 only, so that fetch fails. Add
   **Rules**, **Configuration Rules**, **Create rule**:
   - If: Hostname equals `demo.hecinbox.com`
   - Then: SSL → **Flexible**

   That keeps the rest of the zone on Full (strict) and relaxes only the demo
   hostname. If Configuration Rules are unavailable, the fallback is setting
   the whole zone to Flexible. It is a weaker default, though harmless for
   `hecinbox.com` itself, because a Worker custom domain serves from Cloudflare
   and never performs an origin fetch.
3. Change one line in `public/index.html`, only after the hostname is confirmed
   serving, so the buttons never point at a dead host:
   `const DEMO_URL = "https://demo.hecinbox.com";`

Streamlit holds a long-lived WebSocket. Cloudflare proxies WebSockets on every
plan, including free, so the connection survives the proxy.

### The tradeoff, stated plainly

Visitors get real HTTPS, and the ALB gains Cloudflare's WAF and DDoS protection
in front of it. But the Cloudflare to ALB hop travels the public internet
unencrypted. For a public demo with no login and no sensitive data that is
acceptable. It would not be acceptable for anything handling credentials.

Two follow-ups worth doing when possible:

- Ask the account administrator for `acm:RequestCertificate`,
  `acm:DescribeCertificate` and `acm:ListCertificates`. Then request a
  certificate for `demo.hecinbox.com`, open 443 on `bil6-hecinbox-alb-sg`, add
  the HTTPS listener pointing at `bil6-hecinbox-tg`, and switch the
  Configuration Rule from Flexible to Full (strict). That closes the plaintext
  hop.
- The ALB security group currently allows port 80 from `0.0.0.0/0`, so the
  origin stays reachable directly at its `amazonaws.com` hostname, bypassing
  Cloudflare. Restricting that group to Cloudflare's published IP ranges forces
  all traffic through the proxy. It needs occasional maintenance as those
  ranges change.

Optional: put **Cloudflare Access** in front of `demo.hecinbox.com` if the demo
should be reachable by invitation only. Streamlit has no authentication of its
own, so anything reachable at that URL is reachable by anyone who finds it.

### Status: live and verified

`demo.hecinbox.com` serves the application over HTTPS. Verified end to end:

- valid certificate, Cloudflare Universal SSL, SANs `hecinbox.com` and
  `*.hecinbox.com`
- `/_stcore/health` returns `ok`
- the Streamlit WebSocket upgrades through the proxy, `101 Switching Protocols`
  with a valid `Sec-WebSocket-Accept`
- the app boots in a real browser: title resolves to HECinBox and all nine tabs
  render, so the socket is connected rather than merely accepted

One thing still open: `http://demo.hecinbox.com` answers 200 on plain HTTP
instead of redirecting. Turn on **SSL/TLS**, **Edge Certificates**, **Always
Use HTTPS** so anyone typing the bare hostname is upgraded. The site's own
buttons already point at `https://`, so this only affects hand-typed URLs.

### Note on HSTS

`_headers` sends `Strict-Transport-Security` **without** `includeSubDomains` on
purpose. That directive forces every subdomain to HTTPS in any browser that has
seen the header, and it is remembered for a year, so it is effectively a one
way door. Now that the demo is on HTTPS it would be safe, but enable **Always
Use HTTPS** first and let it settle before adding it.

## Editing the site

Everything is in `public/index.html`. The pieces most likely to change:

- `DEMO_URL`, top of the `<script>` block
- `STAGES`, the seven pipeline steps and their copy
- `SHOTS`, the result figures in the gallery and their captions
- `FAQ`, the question and answer pairs
- `CODE` and `RAW`, the install commands (`CODE` is the highlighted display
  version, `RAW` is what the Copy button puts on the clipboard, so edit both)
- Contact address in the footer, currently `ekahrizi@crimson.ua.edu`

The colour system is CSS custom properties at the top of the `<style>` block.
Changing `--primary` there restyles buttons, links, icons, and charts together.

To regenerate the logo files from a new source image, adjust the paths in the
Pillow snippet below and rerun it from the project root. It knocks the white
background out by flood filling inward from the edges, so enclosed white shapes
such as the whale belly and the Docker mark survive:

```python
from PIL import Image, ImageDraw
im = Image.open("assets/logo.png").convert("RGB")
w, h = im.size
for xy in [(0,0),(w-1,0),(0,h-1),(w-1,h-1)]:
    ImageDraw.floodfill(im, xy, (255,0,255), thresh=26)
im = im.convert("RGBA")
px = im.load()
for y in range(h):
    for x in range(w):
        if px[x,y][:3] == (255,0,255):
            px[x,y] = (255,255,255,0)
im.resize((640,640), Image.LANCZOS).save("public/logo.png", optimize=True)
```

## Before launch

- [ ] Copy the four result maps into `public/screenshots/` (see that folder's README).
      Until they are there, the Results gallery stays hidden.
- [ ] Point `DEMO_URL` at an HTTPS demo host
- [ ] Decide whether the demo needs Cloudflare Access in front of it
- [ ] Optional: replace the social preview image. It currently points at
      `logo.png`, which is square. A purpose built 1200x630 image looks better
      when the link is pasted into Slack, LinkedIn, or X.
