"""把 interview_prep_project/ 下的 7 份 markdown 生成一个可交互的面试练习页面。

markdown 是唯一的内容来源；改了 md 之后重新运行本脚本，再重新发布 artifact。

    uv run --with markdown-it-py python tools/build_artifact.py <输出 html 路径>

需要本机能找到 mmdc（Mermaid CLI），用来把图预渲染成 SVG。
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "artifact_template.html"

PROJECTS = [
    ("mcp", "Identity-Aware MCP", "01_identity_aware_mcp.md"),
    ("portal", "BI Portal", "02_bi_portal_html_host.md"),
    ("rag", "RAG Advisor", "03_rag_call_center_advisor.md"),
    ("etl", "ETL 平台", "04_etl_platform_event_ingestion.md"),
    ("migration", "Fabric 迁移", "05_fabric_migration.md"),
    ("fuam", "Fabric 可观测", "06_fabric_observability.md"),
    ("agent", "Agent 平台", "07_react_agent_platform.md"),
]
FILE_TO_PID = {f: pid for pid, _, f in PROJECTS}

TOPICS = [
    ("opening", "开场讲法"),
    ("identity", "身份与权限"),
    ("tradeoff", "架构取舍"),
    ("reliability", "可靠性与幂等"),
    ("latency", "性能与延迟"),
    ("observability", "可观测与成本"),
    ("incident", "事故与踩坑"),
    ("data", "数据工程"),
    ("ai", "AI / LLM"),
]

# (项目, 题号) → 主题
QUESTION_TOPICS = {
    ("mcp", 1): ["tradeoff", "identity", "ai"],
    ("mcp", 2): ["reliability", "tradeoff", "latency"],
    ("mcp", 3): ["identity", "incident"],
    ("mcp", 4): ["identity", "observability"],
    ("mcp", 5): ["incident", "identity", "ai"],
    ("portal", 1): ["identity", "incident"],
    ("portal", 2): ["identity", "tradeoff"],
    ("portal", 3): ["identity", "tradeoff"],
    ("portal", 4): ["incident"],
    ("portal", 5): ["latency", "observability"],
    ("rag", 1): ["latency", "ai"],
    ("rag", 2): ["ai", "tradeoff"],
    ("rag", 3): ["identity"],
    ("rag", 4): ["ai", "observability"],
    ("rag", 5): ["tradeoff"],
    ("etl", 1): ["data", "tradeoff"],
    ("etl", 2): ["data", "reliability"],
    ("etl", 3): ["data", "identity"],
    ("etl", 4): ["data", "tradeoff", "latency"],
    ("etl", 5): ["reliability"],
    ("migration", 1): ["tradeoff"],
    ("migration", 2): ["reliability", "incident"],
    ("migration", 3): ["identity", "data"],
    ("migration", 4): ["identity"],
    ("migration", 5): ["reliability", "observability"],
    ("fuam", 1): ["observability"],
    ("fuam", 2): ["observability", "ai"],
    ("fuam", 3): ["observability", "incident"],
    ("fuam", 4): ["observability", "data"],
    ("fuam", 5): ["observability", "tradeoff"],
    ("agent", 1): ["ai", "tradeoff"],
    ("agent", 2): ["ai", "reliability", "incident"],
    ("agent", 3): ["ai", "reliability"],
    ("agent", 4): ["identity", "ai"],
    ("agent", 5): ["observability", "ai"],
}

# 发布到网页前去掉的内部标识。按顺序替换，长的在前。
SANITIZE = [
    ("别人给的脚本写 `bqai`，实际是 `biqa.carmax.org`，所有环境都 NXDOMAIN",
     "别人给的脚本里主机名两个字母写反了，所有环境都 NXDOMAIN"),
    ("`biqa` 是 split-horizon DNS 的内部名", "Tableau 主机是 split-horizon DNS 的内部名"),
    ("DATAOPS.METADATA_CONTROLS.INGESTION_PARAMETERS", "ingestion 参数控制表"),
    ("SF_GENERIC_REPLICATION_CONTROL", "复制控制表"),
    ("RETAIL_BUILD<br/>.PAYOFF_SERVICE", "正式表"),
    ("RETAIL_BUILD.PAYOFF_SERVICE", "正式表"),
    ("`EDW_STOCKROOM` 下的 view", "下游 view"),
    ("EDW_STOCKROOM view", "下游 view"),
    ("EDW_STOCKROOM", "下游 view 层"),
    ("一个 `edwproddrhashdeployments` 存储账户，用途是 DR 部署制品", "一个存放 DR 部署制品的存储账户"),
    ("`edwproddrhashdeployments` 存储账户放 DR 部署制品", "名字里带 hash 的存储账户放 DR 部署制品"),
    ("`edwproddrhashdeployments`", "DR 部署制品存储账户"),
    ("edwproddrhashdeployments", "DR 部署制品存储账户"),
    ("Event Hub `payoff-service`", "Payoff 的 Event Hub"),
    ("payoff-service", "Payoff hub"),
    ("`PowerBIProd-Fabric-DataGateway-HA`", "HA 网关集群"),
    ("PowerBIProd-Fabric-DataGateway-HA", "HA 网关集群"),
    ("`KMX-APP-PBIRS-Admins`", "报表管理员 AD 组"),
    ("KMX-APP-PBIRS-Admins", "报表管理员 AD 组"),
    ("`SVCPBIFABRIC`", "Fabric 服务账号"),
    ("SVCPBIFABRIC", "Fabric 服务账号"),
    ("Top 5 报表（Production Dashboard Trend、几个 Merchandising Scorecard、Vehicle Quality Score）",
     "Top 5 报表（都是 scorecard / dashboard 类）"),
]
BANNED = ["biqa", "bqai", "kmx-", "KMX-", "edwprod", "SVCPBI", "DATAOPS", "RETAIL_BUILD",
          "EDW_STOCKROOM", "PowerBIProd", "/Users/"]

md = MarkdownIt("commonmark", {"html": True}).enable("table")


def render(text):
    return md.render(text).strip()


def render_inline(text):
    out = render(text)
    if out.startswith("<p>") and out.endswith("</p>") and out.count("<p>") == 1:
        out = out[3:-4]
    return out


def plain(text):
    t = re.sub(r"`([^`]*)`", r"\1", text)
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]*)\*", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"<br/?>", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def sanitize(text):
    for a, b in SANITIZE:
        text = text.replace(a, b)
    # 文档之间的链接改成页面内跳转
    for f, pid in FILE_TO_PID.items():
        text = re.sub(r"\[[^\]]*\]\(" + re.escape(f) + r"\)", f"[{pid_label(pid)}](#{pid})", text)
    return text


def pid_label(pid):
    return next(label for p, label, _ in PROJECTS if p == pid)


def split_h2(body):
    parts = re.split(r"^## ", body, flags=re.M)
    head = parts[0]
    sections = {}
    for p in parts[1:]:
        title, _, rest = p.partition("\n")
        num = title.split(".")[0].strip()
        sections[num] = (title.strip(), rest.strip().rstrip("-").strip())
    return head, sections


def blockquote_blocks(lines):
    blocks, cur = [], []
    for line in lines:
        if line.startswith(">"):
            cur.append(line)
        else:
            if cur:
                blocks.append(cur)
                cur = []
    if cur:
        blocks.append(cur)
    return blocks


def unquote(block):
    return "\n".join(re.sub(r"^> ?", "", l) for l in block)


def top_level_items(text):
    """取顶层列表项（- 或 1.），连同缩进的续行和嵌套内容。"""
    items, cur = [], None
    for line in text.split("\n"):
        m = re.match(r"^(- |\d+\. )(.*)$", line)
        if m:
            if cur is not None:
                items.append(cur)
            cur = [m.group(2)]
        elif cur is not None and (line.startswith(" ") or line.strip() == ""):
            cur.append(line[2:] if line.startswith("  ") else line.strip())
        elif cur is not None:
            items.append(cur)
            cur = None
    if cur is not None:
        items.append(cur)
    return ["\n".join(i).strip() for i in items]


DIAGRAMS = []


def take_diagrams(text, pid):
    def repl(m):
        idx = len([d for d in DIAGRAMS if d[0] == pid]) + 1
        did = f"d-{pid}-{idx}"
        DIAGRAMS.append((pid, did, m.group(1)))
        return f"\n\nDIAGRAM:{did}\n\n"
    return re.sub(r"```mermaid\n(.*?)```", repl, text, flags=re.S)


def parse_doc(pid, label, fname):
    raw = sanitize((ROOT / fname).read_text(encoding="utf-8"))
    title_line, _, body = raw.partition("\n")
    title = title_line.lstrip("# ").strip()
    head, sections = split_h2(body)

    # 文首：简历原句、一句话、提示
    head_lines = head.strip().rstrip("-").strip().split("\n")
    qblocks = blockquote_blocks(head_lines)
    main = unquote(qblocks[0])
    resume_html = ""
    m = re.search(r"\*\*简历原句[^*]*\*\*(.*?)(?=\n\n\*\*一句话)", main, re.S)
    if m:
        resume_html = render(m.group(1).strip())
    one_zh = re.search(r"\*\*一句话：\*\*\s*(.*)", main).group(1).strip()
    one_en = re.search(r"\*EN:\*\s*(.*)", main).group(1).strip()
    notes, warnings = [], []
    for b in qblocks[1:]:
        text = unquote(b)
        (warnings if "⚠️" in text else notes).append(render(text))

    # 第 1 节：讲法
    _, s1 = sections["1"]
    sub = re.split(r"^### ", s1, flags=re.M)
    pitch_en = pitch_zh = steps_md = numbers_md = ""
    for chunk in sub[1:]:
        h, _, c = chunk.partition("\n")
        c = c.strip()
        if h.startswith("30"):
            en_lines = [l for l in c.split("\n") if l.startswith(">")]
            pitch_en = re.sub(r"^\*EN:\*\s*", "", unquote(en_lines)).strip()
            zm = re.search(r"中文对照：(.*)", c)
            pitch_zh = zm.group(1).strip() if zm else ""
        elif h.startswith("2 分钟"):
            steps_md = c
        elif h.startswith("数字"):
            numbers_md = c
    step_items = top_level_items(steps_md)

    numbers_rows = []
    for line in numbers_md.split("\n"):
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            numbers_rows.append(cells)
    numbers_head, numbers_body = numbers_rows[0], numbers_rows[1:]

    # 第 2 节：deep dive
    _, s2 = sections["2"]
    qa = []
    q_warn = []
    for chunk in re.split(r"^### ", s2, flags=re.M)[1:]:
        h, _, c = chunk.partition("\n")
        hm = re.match(r"Q(\d)\. (.+?)（(.+)）\s*$", h.strip())
        n, q_en, q_zh = int(hm.group(1)), hm.group(2).strip(), hm.group(3).strip()
        zh_part, _, after = c.partition("**EN answer")
        after = after.split("\n", 1)[1] if "\n" in after else ""
        en_blocks = blockquote_blocks(after.strip().split("\n"))
        en = unquote(en_blocks[0]).strip() if en_blocks else ""
        extra = "\n\n".join(unquote(b) for b in en_blocks[1:])
        points_md = top_level_items(zh_part)
        # 去掉 "中文要点" 这类小标题以外的段落，作为补充说明
        lead_blocks = [unquote(b) for b in blockquote_blocks(zh_part.split("\n"))]
        tail = re.findall(r"^\*\*可以主动讲的取舍\*\*.*$", zh_part, re.M)
        for b in lead_blocks:
            if "⚠️" in b:
                q_warn.append({"where": f"Q{n}", "html": render_inline(b)})
        for p in points_md:
            if "⚠️" in p:
                q_warn.append({"where": f"Q{n}", "html": render_inline(p.split("\n")[0])})
        qa.append({
            "n": n,
            "q_en": q_en,
            "q_zh": q_zh,
            "points": [render_inline(p) for p in points_md],
            "points_text": [plain(p) for p in points_md],
            "en": en,
            "lead": "".join(render(b) for b in lead_blocks),
            "tail": "".join(render(t) for t in tail),
            "extra": render(extra) if extra else "",
            "topics": QUESTION_TOPICS[(pid, n)],
        })

    # 第 4 节：项目是干什么的（第 3 节是本机文档链接，不放进网页）
    s4_title, s4 = sections["4"]
    s4 = take_diagrams(s4, pid)
    what_html = render(s4)

    checklist = []
    for w in warnings:
        checklist.append({"where": "总体", "html": w})
    for row in numbers_body:
        if any("⚠️" in c for c in row):
            checklist.append({"where": "数字口径", "html": render_inline(f"**{row[0]}**：{row[1]}" + (f"（{row[2]}）" if len(row) > 2 and row[2] else ""))})
    checklist.extend(q_warn)
    for i, c in enumerate(checklist):
        c["key"] = f"{pid}-{i}"

    context = "\n".join([
        f"Project: {title}",
        "One-liner: " + plain(one_zh) + " / " + plain(one_en),
        "30-second pitch: " + plain(pitch_en),
        "Key facts and numbers:",
        *["- " + " | ".join(plain(c) for c in row) for row in numbers_body],
    ])

    return {
        "id": pid,
        "label": label,
        "title": title,
        "resume": resume_html,
        "one_zh": render_inline(one_zh),
        "one_en": plain(one_en),
        "notes": notes,
        "warnings": warnings,
        "pitch_en": pitch_en,
        "pitch_zh": render_inline(pitch_zh) if pitch_zh else "",
        "steps": [render_inline(s) for s in step_items],
        "steps_text": [plain(s) for s in step_items],
        "numbers_html": render(numbers_md),
        "qa": qa,
        "what_title": s4_title,
        "what_html": what_html,
        "checklist": checklist,
        "context": context,
    }


def render_diagrams(tmp):
    mmdc = shutil.which("mmdc")
    if not mmdc:
        cands = sorted(Path.home().glob(".npm/_npx/*/node_modules/.bin/mmdc"))
        mmdc = str(cands[0]) if cands else None
    if not mmdc:
        sys.exit("找不到 mmdc（Mermaid CLI）")
    cfg = Path(tmp) / "mermaid.json"
    cfg.write_text(json.dumps({
        "theme": "neutral",
        "themeVariables": {"fontFamily": "PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, Arial, sans-serif"},
    }))
    out = {}
    for pid, did, code in DIAGRAMS:
        src = Path(tmp) / f"{did}.mmd"
        dst = Path(tmp) / f"{did}.svg"
        src.write_text(code, encoding="utf-8")
        subprocess.run([mmdc, "-i", str(src), "-o", str(dst), "-I", did, "-b", "transparent",
                        "-c", str(cfg), "-q"], check=True)
        svg = dst.read_text(encoding="utf-8")
        svg = re.sub(r"^<\?xml[^>]*>\s*", "", svg)
        out[did] = svg
    return out


def handbook_html(p, svgs):
    def q_block(q):
        pts = "".join(f"<li>{x}</li>" for x in q["points"])
        return f"""
<details class="hb-q" id="hb-{p['id']}-q{q['n']}">
  <summary><span class="hb-qn">Q{q['n']}</span><span class="hb-qt"><span lang="en">{html.escape(q['q_en'])}</span><span class="hb-qzh">{html.escape(q['q_zh'])}</span></span></summary>
  <div class="hb-qbody">
    {q['lead']}
    <h4>中文要点</h4><ul class="hb-points">{pts}</ul>{q['tail']}
    <h4>EN answer</h4><blockquote lang="en">{html.escape(q['en'])}</blockquote>
    {q['extra']}
    <button type="button" class="btn btn-quiet" data-practice="{p['id']}-q{q['n']}">练这题</button>
  </div>
</details>"""

    what = p["what_html"]
    for did, svg in svgs.items():
        what = what.replace(f"<p>DIAGRAM:{did}</p>", f'<figure class="plate"><div class="plate-scroll">{svg}</div></figure>')
    steps = "".join(f"<li>{s}</li>" for s in p["steps"])
    warn = "".join(f'<div class="callout callout-warn">{w}</div>' for w in p["warnings"])
    notes = "".join(f'<div class="callout">{n}</div>' for n in p["notes"])
    qs = "".join(q_block(q) for q in p["qa"])
    return f"""
<section class="hb" id="hb-{p['id']}" data-pid="{p['id']}" hidden>
  <header class="hb-head">
    <p class="eyebrow">简历原句</p>
    <div class="hb-resume" lang="en">{p['resume']}</div>
    <h2 class="hb-title">{html.escape(p['title'])}</h2>
    <p class="hb-one">{p['one_zh']}</p>
    <p class="hb-one-en" lang="en">{html.escape(p['one_en'])}</p>
    {warn}{notes}
  </header>
  <div class="hb-grid">
    <div class="hb-col">
      <h3>30 秒版本</h3>
      <blockquote class="pitch" lang="en">{html.escape(p['pitch_en'])}</blockquote>
      <p class="hb-zh">{p['pitch_zh']}</p>
      <button type="button" class="btn btn-quiet" data-practice="{p['id']}-p">练 30 秒开场</button>
      <h3>2 分钟展开</h3>
      <ol class="hb-steps">{steps}</ol>
    </div>
    <div class="hb-col">
      <h3>数字和口径</h3>
      <div class="table-scroll">{p['numbers_html']}</div>
    </div>
  </div>
  <h3>Deep dive</h3>
  <div class="hb-qs">{qs}</div>
  <h3>{html.escape(p['what_title'].split('. ', 1)[-1])}</h3>
  <div class="hb-what">{what}</div>
</section>"""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out_path = Path(sys.argv[1])
    projects = [parse_doc(pid, label, f) for pid, label, f in PROJECTS]

    with tempfile.TemporaryDirectory() as tmp:
        svgs = render_diagrams(tmp)

    items = []
    for p in projects:
        items.append({
            "id": f"{p['id']}-p", "pid": p["id"], "kind": "pitch", "label": "30 秒开场",
            "q_en": "Give me the 30-second version of this project.",
            "q_zh": "用 30 秒讲清楚这个项目",
            "points": p["steps"], "points_text": p["steps_text"],
            "en": p["pitch_en"], "extra": "", "topics": ["opening"], "target": 30,
        })
        for q in p["qa"]:
            items.append({
                "id": f"{p['id']}-q{q['n']}", "pid": p["id"], "kind": "q", "label": f"Q{q['n']}",
                "q_en": q["q_en"], "q_zh": q["q_zh"], "points": q["points"],
                "points_text": q["points_text"], "en": q["en"], "extra": q["extra"],
                "topics": q["topics"], "target": 120,
            })

    data = {
        "projects": [{k: p[k] for k in ("id", "label", "title", "one_zh", "one_en", "context", "checklist")} for p in projects],
        "items": items,
        "topics": [{"id": t, "label": l} for t, l in TOPICS],
    }
    hb = "".join(handbook_html(p, {d: s for d, s in svgs.items() if d.startswith(f"d-{p['id']}-")}) for p in projects)

    page = TEMPLATE.read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("/*__DATA__*/", data_json).replace("<!--__HANDBOOK__-->", hb)

    leaks = [b for b in BANNED if b in page]
    if leaks:
        sys.exit(f"页面里还有内部标识：{leaks}")
    out_path.write_text(page, encoding="utf-8")
    print(f"wrote {out_path} ({len(page)/1024:.0f} KB, {len(items)} items, {len(svgs)} diagrams)")


if __name__ == "__main__":
    main()
