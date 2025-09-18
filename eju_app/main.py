import os
import sqlite3
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="EJU Quiz App")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "questions.db")

# Set up Jinja2 templates directory
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Constants used throughout the app
DEFAULT_SECTIONS: List[str] = ["日语", "综合科目", "理科", "未分类"]
CORRECT_OPTION_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("A", "A"),
    ("B", "B"),
    ("C", "C"),
    ("D", "D"),
)


def get_db():
    """Return a new database connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the questions table if it does not already exist, and add section column if missing."""
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL,
                tags TEXT,
                section TEXT
            );
            """
        )
        # Ensure the section column exists even if the table was created earlier
        cur = conn.execute("PRAGMA table_info(questions)")
        columns = [row[1] for row in cur.fetchall()]
        if "section" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN section TEXT")
        conn.commit()


@app.on_event("startup")
def on_startup():
    """Ensure the database is initialized when the app starts."""
    init_db()


def parse_tags(tags_value: Optional[str]) -> List[str]:
    """Split a comma-separated tag string into a cleaned list."""

    if not tags_value:
        return []
    tags: List[str] = []
    for tag in tags_value.split(","):
        cleaned = tag.strip()
        if cleaned:
            tags.append(cleaned)
    return tags


def normalize_tags_string(tags_value: str) -> str:
    """Normalize tag input by trimming whitespace and removing duplicates while keeping order."""

    seen = set()
    normalized: List[str] = []
    for tag in parse_tags(tags_value):
        if tag not in seen:
            normalized.append(tag)
            seen.add(tag)
    return ", ".join(normalized)


def build_tag_tree(tags: List[str]) -> Dict[str, Any]:
    """Convert a list of slash-separated tags into a nested dictionary tree."""

    tree: Dict[str, Dict[str, Any]] = {}
    for tag in tags:
        parts = [p.strip() for p in tag.split("/") if p.strip()]
        if not parts:
            continue
        node = tree
        for part in parts:
            node = node.setdefault(part, {})  # type: ignore[assignment]
    return tree


def build_tag_options(tags: List[str]) -> List[Dict[str, Any]]:
    """Build select options (value, label, level) from a tag list."""

    tree = build_tag_tree(tags)

    def walk(node: Dict[str, Any], prefix: str = "", level: int = 0) -> List[Dict[str, Any]]:
        options: List[Dict[str, Any]] = []
        for key in sorted(node.keys()):
            value = f"{prefix}/{key}" if prefix else key
            options.append({"value": value, "label": key, "level": level})
            options.extend(walk(node[key], value, level + 1))
        return options

    return walk(tree)


def format_tag_label(tag_value: Optional[str]) -> str:
    """Format a tag path for display."""

    if not tag_value:
        return ""
    parts = [p.strip() for p in tag_value.split("/") if p.strip()]
    return " / ".join(parts)


def gather_sections_metadata(
    conn: sqlite3.Connection,
) -> Tuple[List[Dict[str, Any]], int, List[str]]:
    """Return section overview (count and top tags), total question count and global hot tags."""

    stats: Dict[str, Dict[str, Any]] = {}
    total_questions = 0
    overall_tags: Counter[str] = Counter()
    cur = conn.execute("SELECT section, tags FROM questions")
    for row in cur.fetchall():
        raw_section = row["section"] or "未分类"
        section_name = raw_section.strip() or "未分类"
        entry = stats.setdefault(
            section_name,
            {"count": 0, "tags": Counter()},
        )
        entry["count"] += 1
        total_questions += 1
        tag_list = parse_tags(row["tags"])
        entry["tags"].update(tag_list)
        overall_tags.update(tag_list)

    sections: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def build_section_entry(name: str) -> Dict[str, Any]:
        entry = stats.get(name, {"count": 0, "tags": Counter()})
        top_tags = [tag for tag, _ in entry["tags"].most_common(4)]
        return {
            "name": name,
            "display_name": name,
            "count": entry["count"],
            "top_tags": top_tags,
        }

    for name in DEFAULT_SECTIONS:
        sections.append(build_section_entry(name))
        seen.add(name)

    for name in sorted(stats.keys()):
        if name in seen:
            continue
        sections.append(build_section_entry(name))
        seen.add(name)

    top_tags = [tag for tag, _ in overall_tags.most_common(12)]

    return sections, total_questions, top_tags


def get_section_choices(conn: sqlite3.Connection) -> List[str]:
    """Return section choices for forms (defaults + existing unique sections)."""

    cur = conn.execute(
        "SELECT DISTINCT section FROM questions WHERE section IS NOT NULL AND trim(section) <> ''"
    )
    existing = [row["section"] for row in cur.fetchall() if row["section"]]
    seen: Set[str] = set()
    choices: List[str] = []
    for name in DEFAULT_SECTIONS:
        if name not in seen:
            choices.append(name)
            seen.add(name)
    for name in existing:
        if name not in seen:
            choices.append(name)
            seen.add(name)
    return choices


def make_feedback(msg: Optional[str], category: Optional[str]) -> List[Dict[str, str]]:
    """Build feedback messages for the template."""

    if not msg:
        return []
    normalized = category if category in {"success", "error", "info"} else "info"
    return [{"category": normalized, "text": msg}]


def parse_form_body(body_bytes: bytes) -> Dict[str, str]:
    """Parse URL-encoded form body into a simple dict."""

    parsed = urllib.parse.parse_qs(body_bytes.decode())
    return {key: (values[0].strip() if values else "") for key, values in parsed.items()}


def validate_question_payload(data: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
    """Validate and normalize question payload from a form."""

    errors: List[str] = []

    question = data.get("question", "").strip()
    option_a = data.get("option_a", "").strip()
    option_b = data.get("option_b", "").strip()
    option_c = data.get("option_c", "").strip()
    option_d = data.get("option_d", "").strip()
    correct_option = data.get("correct_option", "").strip().upper()
    section = data.get("section", "").strip()
    tags = data.get("tags", "")

    if not question:
        errors.append("题目内容不能为空。")
    if not option_a or not option_b or not option_c or not option_d:
        errors.append("请填写四个选项内容。")
    if correct_option not in {choice[0] for choice in CORRECT_OPTION_CHOICES}:
        errors.append("请选择正确答案。")
    if not section:
        errors.append("请选择题目所属的板块。")

    normalized_tags = normalize_tags_string(tags)

    payload = {
        "question": question,
        "option_a": option_a,
        "option_b": option_b,
        "option_c": option_c,
        "option_d": option_d,
        "correct_option": correct_option,
        "section": section,
        "tags": normalized_tags,
    }
    return errors, payload


def enrich_question_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a question row into a dict enriched with parsed tags."""

    data = dict(row)
    data["section"] = data.get("section") or "未分类"
    data["tag_list"] = parse_tags(data.get("tags"))
    return data


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page: let the user choose a section."""

    with get_db() as conn:
        sections, total_questions, top_tags = gather_sections_metadata(conn)

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "sections": sections,
            "total_questions": total_questions,
            "top_tags": top_tags,
            "feedback": [],
        },
    )


@app.get("/quiz", response_class=HTMLResponse)
async def quiz(
    request: Request,
    tag: Optional[str] = None,
    section: Optional[str] = None,
    search: Optional[str] = None,
):
    """Display the quiz page with optional section, tag and keyword filters."""

    tag_filter = (tag or "").strip()
    section_filter = (section or "").strip()
    search_filter = (search or "").strip()

    with get_db() as conn:
        base_query = "SELECT * FROM questions"
        conditions: List[str] = []
        params: List[Any] = []
        if section_filter:
            conditions.append("section = ?")
            params.append(section_filter)
        if tag_filter:
            conditions.append("lower(tags) LIKE lower(?)")
            params.append(f"%{tag_filter}%")
        if search_filter:
            like_value = f"%{search_filter}%"
            conditions.append(
                "(" "question LIKE ? OR option_a LIKE ? OR option_b LIKE ? OR option_c LIKE ? OR option_d LIKE ? OR tags LIKE ?" ")"
            )
            params.extend([like_value] * 6)
        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)
        base_query += " ORDER BY id"
        cur = conn.execute(base_query, tuple(params))
        questions_raw = cur.fetchall()
        questions = [enrich_question_row(row) for row in questions_raw]

        # Collect all tags (within the selected section, if any)
        tag_query = "SELECT tags FROM questions"
        tag_conditions: List[str] = []
        tag_params: List[Any] = []
        if section_filter:
            tag_conditions.append("section = ?")
            tag_params.append(section_filter)
        if tag_conditions:
            tag_query += " WHERE " + " AND ".join(tag_conditions)
        tag_cur = conn.execute(tag_query, tuple(tag_params))
        tags_all: List[str] = []
        for row in tag_cur.fetchall():
            tags_all.extend(parse_tags(row["tags"]))
        tags_unique = sorted(set(tags_all))
        tag_options = build_tag_options(tags_unique)

        sections_metadata, _, _ = gather_sections_metadata(conn)

    selected_section_display = section_filter or "全部题目"

    return templates.TemplateResponse(
        "quiz.html",
        {
            "request": request,
            "questions": questions,
            "tag_options": tag_options,
            "selected_tag": tag_filter,
            "selected_tag_label": format_tag_label(tag_filter),
            "selected_section": section_filter,
            "selected_section_display": selected_section_display,
            "sections": sections_metadata,
            "search": search_filter,
            "feedback": [],
        },
    )


@app.post("/quiz", response_class=HTMLResponse)
async def submit_quiz(request: Request):
    """Handle quiz submission and compute results."""
    # Read raw body and parse URL-encoded form manually to avoid python-multipart dependency
    body_bytes = await request.body()
    parsed = urllib.parse.parse_qs(body_bytes.decode())
    answers: Dict[str, str] = {}
    for key, values in parsed.items():
        if key.startswith("question-") and values:
            qid = key.split("-", 1)[1]
            answers[qid] = values[0]
    if not answers:
        # If no answers were submitted, redirect back to quiz
        return RedirectResponse(url="/quiz", status_code=status.HTTP_303_SEE_OTHER)

    placeholders = ",".join("?" for _ in answers)
    with get_db() as conn:
        query = (
            "SELECT id, question, option_a, option_b, option_c, option_d, "
            "correct_option, tags, section FROM questions WHERE id IN ({})"
        ).format(placeholders)
        cur = conn.execute(query, list(answers.keys()))
        result_set = cur.fetchall()

    total = len(result_set)
    correct = 0
    details = []
    for row in result_set:
        qid = str(row["id"])
        correct_answer = row["correct_option"]
        user_answer = answers.get(qid)
        is_correct = user_answer == correct_answer
        if is_correct:
            correct += 1
        details.append(
            {
                "id": qid,
                "question": row["question"],
                "options": [
                    ("A", row["option_a"]),
                    ("B", row["option_b"]),
                    ("C", row["option_c"]),
                    ("D", row["option_d"]),
                ],
                "user_answer": user_answer,
                "correct_answer": correct_answer,
                "is_correct": is_correct,
                "section": row["section"] or "未分类",
                "tag_list": parse_tags(row["tags"]),
            }
        )
    score_percent = 0.0
    if total > 0:
        score_percent = round((correct / total) * 100.0, 2)
    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "total": total,
            "correct": correct,
            "score": score_percent,
            "details": details,
            "feedback": [],
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(
    request: Request,
    msg: Optional[str] = None,
    category: Optional[str] = None,
    section: Optional[str] = None,
    tag: Optional[str] = None,
    search: Optional[str] = None,
):
    """Display the admin interface with a list of questions."""

    tag_filter = (tag or "").strip()
    section_filter = (section or "").strip()
    search_filter = (search or "").strip()

    with get_db() as conn:
        base_query = "SELECT * FROM questions"
        conditions: List[str] = []
        params: List[Any] = []
        if section_filter:
            conditions.append("section = ?")
            params.append(section_filter)
        if tag_filter:
            conditions.append("lower(tags) LIKE lower(?)")
            params.append(f"%{tag_filter}%")
        if search_filter:
            like_value = f"%{search_filter}%"
            conditions.append(
                "(" "question LIKE ? OR option_a LIKE ? OR option_b LIKE ? OR option_c LIKE ? OR option_d LIKE ? OR tags LIKE ?" ")"
            )
            params.extend([like_value] * 6)
        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)
        base_query += " ORDER BY id"
        cur = conn.execute(base_query, tuple(params))
        questions_raw = cur.fetchall()
        questions = [enrich_question_row(row) for row in questions_raw]

        sections_metadata, _, _ = gather_sections_metadata(conn)

        tag_query = "SELECT tags FROM questions"
        tag_conditions: List[str] = []
        tag_params: List[Any] = []
        if section_filter:
            tag_conditions.append("section = ?")
            tag_params.append(section_filter)
        if tag_conditions:
            tag_query += " WHERE " + " AND ".join(tag_conditions)
        tag_cur = conn.execute(tag_query, tuple(tag_params))
        tags_all: List[str] = []
        for row in tag_cur.fetchall():
            tags_all.extend(parse_tags(row["tags"]))
        tags_unique = sorted(set(tags_all))
        tag_options = build_tag_options(tags_unique)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "questions": questions,
            "sections": sections_metadata,
            "tag_options": tag_options,
            "selected_section": section_filter,
            "selected_tag": tag_filter,
            "search": search_filter,
            "feedback": make_feedback(msg, category),
        },
    )


@app.get("/admin/add", response_class=HTMLResponse)
async def add_question_get(request: Request):
    """Render the form to add a new question."""
    with get_db() as conn:
        section_choices = get_section_choices(conn)

    return templates.TemplateResponse(
        "add_edit.html",
        {
            "request": request,
            "action": "add",
            "question_data": None,
            "form_errors": [],
            "correct_option_choices": CORRECT_OPTION_CHOICES,
            "section_choices": section_choices,
            "feedback": [],
        },
    )


@app.post("/admin/add", response_class=HTMLResponse)
async def add_question_post(request: Request):
    """Handle submission of a new question."""
    body_bytes = await request.body()
    form_data = parse_form_body(body_bytes)
    errors, payload = validate_question_payload(form_data)

    with get_db() as conn:
        section_choices = get_section_choices(conn)
        if errors:
            return templates.TemplateResponse(
                "add_edit.html",
                {
                    "request": request,
                    "action": "add",
                    "question_data": payload,
                    "form_errors": errors,
                    "correct_option_choices": CORRECT_OPTION_CHOICES,
                    "section_choices": section_choices,
                    "feedback": [],
                },
                status_code=400,
            )

        conn.execute(
            "INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct_option, tags, section) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload["question"],
                payload["option_a"],
                payload["option_b"],
                payload["option_c"],
                payload["option_d"],
                payload["correct_option"],
                payload["tags"],
                payload["section"],
            ),
        )
        conn.commit()

    admin_url = request.url_for("admin") + "?msg=题目已添加&category=success"
    return RedirectResponse(url=admin_url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/edit/{qid}", response_class=HTMLResponse)
async def edit_question_get(request: Request, qid: int):
    """Render the form to edit an existing question."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM questions WHERE id=?", (qid,))
        row = cur.fetchone()
    if row is None:
        return RedirectResponse(
            url=f"{request.url_for('admin')}?msg=题目不存在&category=error",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    question_data = {
        "id": row["id"],
        "question": row["question"],
        "option_a": row["option_a"],
        "option_b": row["option_b"],
        "option_c": row["option_c"],
        "option_d": row["option_d"],
        "correct_option": row["correct_option"],
        "tags": row["tags"] or "",
        "section": row["section"] or "",
    }
    with get_db() as conn:
        section_choices = get_section_choices(conn)
    return templates.TemplateResponse(
        "add_edit.html",
        {
            "request": request,
            "action": "edit",
            "question_data": question_data,
            "form_errors": [],
            "correct_option_choices": CORRECT_OPTION_CHOICES,
            "section_choices": section_choices,
            "feedback": [],
        },
    )


@app.post("/admin/edit/{qid}", response_class=HTMLResponse)
async def edit_question_post(request: Request, qid: int):
    """Handle submission of edits to an existing question."""
    body_bytes = await request.body()
    form_data = parse_form_body(body_bytes)
    errors, payload = validate_question_payload(form_data)

    with get_db() as conn:
        section_choices = get_section_choices(conn)
        if errors:
            payload["id"] = qid
            return templates.TemplateResponse(
                "add_edit.html",
                {
                    "request": request,
                    "action": "edit",
                    "question_data": payload,
                    "form_errors": errors,
                    "correct_option_choices": CORRECT_OPTION_CHOICES,
                    "section_choices": section_choices,
                    "feedback": [],
                },
                status_code=400,
            )

        conn.execute(
            "UPDATE questions SET question=?, option_a=?, option_b=?, option_c=?, option_d=?, correct_option=?, tags=?, section=? WHERE id=?",
            (
                payload["question"],
                payload["option_a"],
                payload["option_b"],
                payload["option_c"],
                payload["option_d"],
                payload["correct_option"],
                payload["tags"],
                payload["section"],
                qid,
            ),
        )
        conn.commit()

    admin_url = request.url_for("admin") + "?msg=题目已更新&category=success"
    return RedirectResponse(url=admin_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/delete/{qid}", response_class=HTMLResponse)
async def delete_question(request: Request, qid: int):
    """Delete a question from the database."""
    with get_db() as conn:
        conn.execute("DELETE FROM questions WHERE id=?", (qid,))
        conn.commit()
    admin_url = request.url_for("admin") + "?msg=题目已删除&category=success"
    return RedirectResponse(url=admin_url, status_code=status.HTTP_303_SEE_OTHER)