import io
import re
import base64
from pathlib import Path
from typing import List, Optional
from github import Github, GithubException, InputGitTreeElement


# ─────────────────────────────────────────────────────────────────────────────
# Image compression helper — keeps images under 150 KB for fast GH Pages deploy
# ─────────────────────────────────────────────────────────────────────────────
def _compress_image(raw: bytes, max_bytes: int = 150_000) -> bytes:
    """
    Compress a PNG/JPEG image to stay under max_bytes.
    Uses Pillow if available, otherwise returns raw bytes unchanged.
    """
    if len(raw) <= max_bytes:
        return raw
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        quality = 85
        while quality >= 20:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue()
            quality -= 10
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=20, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper: get or initialise a repo + branch ref
# ─────────────────────────────────────────────────────────────────────────────
def _get_repo_and_ref(g: "Github", repo_name: str, branch: str):
    try:
        repo = g.get_repo(repo_name)
    except GithubException as e:
        raise Exception(f"Could not access repository '{repo_name}': {e.data.get('message', str(e))}")

    try:
        repo.get_contents("/")
    except GithubException as e:
        if e.status == 404:
            repo.create_file(
                "README.md",
                "Initial commit",
                "# Blog Repository\n\nAutomatically generated.",
                branch=branch,
            )
        else:
            raise e

    try:
        ref = repo.get_git_ref(f"heads/{branch}")
        base_tree = repo.get_git_tree(ref.object.sha)
    except GithubException:
        try:
            main_ref = repo.get_git_ref("heads/main")
        except GithubException:
            main_ref = repo.get_git_ref("heads/master")
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=main_ref.object.sha)
        ref = repo.get_git_ref(f"heads/{branch}")
        base_tree = repo.get_git_tree(ref.object.sha)

    return repo, ref, base_tree


# ─────────────────────────────────────────────────────────────────────────────
# 1. Original function — push .md + images to blog repo (unchanged behaviour)
# ─────────────────────────────────────────────────────────────────────────────
def push_to_github(
    repo_name: str,
    branch: str,
    token: str,
    md_content: str,
    md_filename: str,
    images_dir: str = "images",
    commit_message: str = "Add blog post",
) -> str:
    """
    Pushes a markdown file and its referenced images to a GitHub repository.
    """
    g = Github(token)
    repo, ref, base_tree = _get_repo_and_ref(g, repo_name, branch)

    elements = []
    elements.append(InputGitTreeElement(
        path=md_filename,
        mode="100644",
        type="blob",
        content=md_content,
    ))

    img_matches = re.findall(r"!\[.*?\]\((images/.*?)\)", md_content)
    for img_path_str in img_matches:
        img_path = Path(img_path_str)
        if img_path.exists():
            with open(img_path, "rb") as f:
                raw = f.read()
            blob = repo.create_git_blob(base64.b64encode(raw).decode("utf-8"), "base64")
            elements.append(InputGitTreeElement(
                path=img_path_str,
                mode="100644",
                type="blob",
                sha=blob.sha,
            ))

    new_tree = repo.create_git_tree(elements, base_tree)
    parent   = repo.get_git_commit(ref.object.sha)
    commit   = repo.create_git_commit(commit_message, new_tree, [parent])
    ref.edit(commit.sha)
    return f"https://github.com/{repo_name}/blob/{branch}/{md_filename}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Markdown → Design B HTML converter
# ─────────────────────────────────────────────────────────────────────────────
def _md_to_html(md: str, blog_title: str, slug: str) -> str:
    """Convert markdown to a full Design B styled HTML page."""

    lede = ""
    body_lines = []
    past_title = False
    for line in md.splitlines():
        if line.startswith("# ") and not past_title:
            past_title = True
            continue
        if past_title and line.strip() and not lede and not line.startswith("#"):
            lede = line.strip()
        else:
            body_lines.append(line)

    body_md = "\n".join(body_lines)

    def _inline_md(text: str) -> str:
        text = re.sub(r"\*\*\*(.*?)\*\*\*", r"<strong><em>\1</em></strong>", text)
        text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*(.*?)\*", r"<em>\1</em>", text)
        text = re.sub(r"`(.*?)`", r"<code>\1</code>", text)
        text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
        return text

    def convert_md_body(text: str) -> str:
        html_parts = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]

            img_match = re.match(r"!\[(.*?)\]\((images/.*?)\)", line.strip())
            if img_match:
                alt = img_match.group(1)
                src = img_match.group(2)
                img_filename = Path(src).name
                html_parts.append(
                    f'<figure class="blog-figure">'
                    f'<img src="images/{img_filename}" alt="{alt}" class="blog-img">'
                    f'<figcaption>{alt}</figcaption></figure>'
                )
                i += 1
                continue

            if line.startswith("## "):
                html_parts.append(f'<h2 class="blog-h2">{line[3:].strip()}</h2>')
                i += 1
                continue

            if line.startswith("### "):
                html_parts.append(f'<h3 class="blog-h3">{line[4:].strip()}</h3>')
                i += 1
                continue

            if line.startswith("- ") or line.startswith("* "):
                items = []
                while i < len(lines) and (lines[i].startswith("- ") or lines[i].startswith("* ")):
                    items.append(f'<li>{_inline_md(lines[i][2:])}</li>')
                    i += 1
                html_parts.append(f'<ul class="blog-ul">{"".join(items)}</ul>')
                continue

            if re.match(r"^\d+\. ", line):
                items = []
                while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                    items.append(f'<li>{_inline_md(re.sub(r"^\d+\. ", "", lines[i]))}</li>')
                    i += 1
                html_parts.append(f'<ol class="blog-ol">{"".join(items)}</ol>')
                continue

            if line.startswith("> "):
                html_parts.append(f'<blockquote class="blog-callout">{_inline_md(line[2:])}</blockquote>')
                i += 1
                continue

            if line.strip() in ("---", "***", "___"):
                html_parts.append('<hr class="blog-hr">')
                i += 1
                continue

            if not line.strip():
                i += 1
                continue

            html_parts.append(f'<p class="blog-p">{_inline_md(line.strip())}</p>')
            i += 1

        return "\n".join(html_parts)

    body_html = convert_md_body(body_md)
    lede_html = _inline_md(lede) if lede else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{blog_title} — Ashu Mishra</title>
<meta name="description" content="{lede[:160] if lede else blog_title}">
<meta property="og:title" content="{blog_title}">
<meta property="og:description" content="{lede[:200] if lede else ''}">
<meta property="og:type" content="article">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300;0,400;0,600;0,700;1,300;1,600&family=Plus+Jakarta+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f9f8f5;--surface:#ffffff;--border:#e8e6df;--border2:#d4d1c8;
  --accent:#1a1a2e;--accent2:#e85d26;--text:#1a1a2e;--muted:#6b6878;
  --faint:#a8a5b0;--tag-bg:#f0ede6;
  --font-display:'Fraunces',serif;--font-body:'Plus Jakarta Sans',sans-serif;
  --font-mono:'JetBrains Mono',monospace;--font-serif:Georgia,serif;
  --content-width:720px;
}}
html{{scroll-behavior:smooth}}
body{{background:var(--bg);color:var(--text);font-family:var(--font-body);font-size:16px;line-height:1.7;overflow-x:hidden}}
nav{{position:fixed;top:0;left:0;right:0;z-index:100;display:flex;align-items:center;justify-content:space-between;padding:16px 56px;background:rgba(249,248,245,0.95);backdrop-filter:blur(16px);border-bottom:1px solid var(--border)}}
.nav-logo{{font-family:var(--font-display);font-weight:700;font-size:20px;letter-spacing:-0.5px;color:var(--text);text-decoration:none}}
.nav-logo span{{color:var(--accent2)}}
.nav-back{{font-family:var(--font-mono);font-size:11px;color:var(--muted);text-decoration:none;letter-spacing:.04em;transition:color .2s}}
.nav-back:hover{{color:var(--accent2)}}
.blog-hero{{max-width:var(--content-width);margin:0 auto;padding:120px 24px 40px}}
.blog-tag-row{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.blog-tag{{font-family:var(--font-mono);font-size:10px;color:var(--muted);background:var(--tag-bg);border:1px solid var(--border);padding:3px 10px;border-radius:20px;letter-spacing:.03em}}
.blog-title{{font-family:var(--font-display);font-size:clamp(28px,4vw,46px);font-weight:700;line-height:1.1;letter-spacing:-1.5px;color:var(--text);margin-bottom:20px}}
.blog-meta{{font-family:var(--font-mono);font-size:11px;color:var(--faint);margin-bottom:32px;display:flex;align-items:center;gap:12px}}
.blog-meta-dot{{width:3px;height:3px;border-radius:50%;background:var(--faint)}}
.blog-lede{{font-size:18px;color:var(--muted);line-height:1.75;font-style:italic;padding-bottom:32px;border-bottom:1px solid var(--border);font-family:var(--font-serif)}}
.blog-body{{max-width:var(--content-width);margin:0 auto;padding:40px 24px 96px}}
.blog-h2{{font-family:var(--font-display);font-size:clamp(20px,2.5vw,28px);font-weight:700;letter-spacing:-.6px;color:var(--text);margin:48px 0 16px;line-height:1.2}}
.blog-h3{{font-family:var(--font-display);font-size:clamp(16px,2vw,20px);font-weight:600;letter-spacing:-.3px;color:var(--text);margin:32px 0 12px}}
.blog-p{{font-size:16px;color:#2a2a3e;line-height:1.85;margin-bottom:20px;font-family:var(--font-serif)}}
.blog-p strong{{color:var(--text);font-weight:700}}
.blog-p em{{font-style:italic}}
.blog-p code{{font-family:var(--font-mono);font-size:13px;background:var(--tag-bg);padding:2px 6px;border-radius:3px;color:var(--accent2)}}
.blog-p a{{color:var(--accent2);text-decoration:none;border-bottom:1px solid rgba(232,93,38,.3);transition:border-color .2s}}
.blog-p a:hover{{border-color:var(--accent2)}}
.blog-ul,.blog-ol{{margin:0 0 20px 0;padding-left:24px;font-family:var(--font-serif);font-size:16px;color:#2a2a3e;line-height:1.8}}
.blog-ul li,.blog-ol li{{margin-bottom:8px}}
.blog-ul li strong,.blog-ol li strong{{color:var(--text);font-weight:700}}
.blog-callout{{border-left:3px solid var(--accent2);background:rgba(232,93,38,.04);padding:16px 20px;margin:24px 0;font-family:var(--font-serif);font-size:15px;color:var(--muted);line-height:1.75;border-radius:0}}
.blog-hr{{border:none;border-top:1px solid var(--border);margin:40px 0}}
.blog-figure{{margin:32px 0;text-align:center}}
.blog-img{{max-width:100%;border-radius:8px;border:1px solid var(--border)}}
.blog-figure figcaption{{font-family:var(--font-mono);font-size:11px;color:var(--faint);margin-top:10px;letter-spacing:.03em}}
code{{font-family:var(--font-mono);font-size:13px;background:var(--tag-bg);padding:2px 6px;border-radius:3px;color:var(--accent2)}}
a{{color:var(--accent2);text-decoration:none}}
a:hover{{text-decoration:underline}}
.blog-footer{{border-top:1px solid var(--border);padding:24px 56px;display:flex;justify-content:space-between;align-items:center}}
.blog-footer p{{font-family:var(--font-mono);font-size:11px;color:var(--faint)}}
.blog-footer a{{color:var(--faint);text-decoration:none;transition:color .2s}}
.blog-footer a:hover{{color:var(--accent2)}}
@media(max-width:768px){{
  nav{{padding:14px 20px}}
  .blog-hero{{padding:100px 20px 32px}}
  .blog-body{{padding:32px 20px 64px}}
  .blog-footer{{flex-direction:column;gap:8px;text-align:center;padding:20px}}
}}
</style>
</head>
<body>
<nav>
  <a href="../../" class="nav-logo">AM<span>.</span></a>
  <a href="../" class="nav-back">← All Blogs</a>
</nav>
<div class="blog-hero">
  <div class="blog-tag-row">
    <span class="blog-tag">Product Management</span>
    <span class="blog-tag">AI / ML</span>
  </div>
  <h1 class="blog-title">{blog_title}</h1>
  <div class="blog-meta">
    <span>Ashu Mishra</span>
    <span class="blog-meta-dot"></span>
    <span>ashumishra.co.in</span>
  </div>
  {f'<p class="blog-lede">{lede_html}</p>' if lede_html else ''}
</div>
<div class="blog-body">
{body_html}
</div>
<footer class="blog-footer">
  <p>© 2026 Ashu Mishra · Delhi, India</p>
  <p><a href="../">← Back to Blogs</a></p>
</footer>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# 3. Push rendered HTML + compressed images to website repo
# ─────────────────────────────────────────────────────────────────────────────
def push_blog_to_website(
    website_repo_name: str,
    branch: str,
    token: str,
    md_content: str,
    blog_title: str,
    slug: str,
    commit_message: str = "Add blog post to website",
) -> str:
    """
    Converts the markdown blog to Design B HTML and pushes it to the
    GitHub Pages website repo under blogs/<slug>/index.html.
    Images are compressed to <150KB then pushed to blogs/<slug>/images/*.
    Returns the live URL of the blog post.
    """
    g = Github(token)
    repo, ref, base_tree = _get_repo_and_ref(g, website_repo_name, branch)

    elements = []

    # HTML page
    html_content = _md_to_html(md_content, blog_title, slug)
    elements.append(InputGitTreeElement(
        path=f"blogs/{slug}/index.html",
        mode="100644",
        type="blob",
        content=html_content,
    ))

    # Images — compress before pushing so GH Pages deploy never times out
    img_matches = re.findall(r"!\[.*?\]\((images/.*?)\)", md_content)
    seen = set()
    for img_path_str in img_matches:
        img_path = Path(img_path_str)
        if not img_path.exists() or img_path.name in seen:
            continue
        seen.add(img_path.name)
        with open(img_path, "rb") as f:
            raw = f.read()
        compressed = _compress_image(raw)
        blob = repo.create_git_blob(base64.b64encode(compressed).decode("utf-8"), "base64")
        elements.append(InputGitTreeElement(
            path=f"blogs/{slug}/images/{img_path.name}",
            mode="100644",
            type="blob",
            sha=blob.sha,
        ))

    new_tree = repo.create_git_tree(elements, base_tree)
    parent   = repo.get_git_commit(ref.object.sha)
    commit   = repo.create_git_commit(commit_message, new_tree, [parent])
    ref.edit(commit.sha)

    return f"https://ashumishra.co.in/blogs/{slug}/"
