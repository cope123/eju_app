import os
import sqlite3
from typing import Optional, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import urllib.parse

app = FastAPI(title="EJU Quiz App")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "questions.db")

# Set up Jinja2 templates directory
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


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


def build_tag_hierarchy(tags: List[str]) -> Dict[str, List[str]]:
    """
    Build a hierarchical dictionary of tags from a flat list. Tags with slashes indicate parent/child relationships.
    Example: ["数学/代数", "数学/几何", "日语"] -> {"数学": ["代数", "几何"], "日语": []}
    """
    hierarchy: Dict[str, List[str]] = {}
    for tag in tags:
        parts = [p.strip() for p in tag.split("/") if p.strip()]
        if not parts:
            continue
        parent = parts[0]
        if len(parts) == 1:
            hierarchy.setdefault(parent, [])
        else:
            child = "/".join(parts[1:])
            hierarchy.setdefault(parent, [])
            if child not in hierarchy[parent]:
                hierarchy[parent].append(child)
    # Sort children and parents for consistent ordering
    for parent in hierarchy:
        hierarchy[parent].sort()
    return dict(sorted(hierarchy.items()))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page: let the user choose a section."""
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/quiz", response_class=HTMLResponse)
async def quiz(
    request: Request,
    tag: Optional[str] = None,
    section: Optional[str] = None,
):
    """Display the quiz page with optional section and tag filters."""
    with get_db() as conn:
        base_query = "SELECT * FROM questions"
        conditions = []
        params = []
        if section:
            conditions.append("section = ?")
            params.append(section)
        if tag:
            conditions.append("lower(tags) LIKE lower(?)")
            params.append(f"%{tag}%")
        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)
        base_query += " ORDER BY id"
        cur = conn.execute(base_query, tuple(params))
        questions = cur.fetchall()

        # Collect all tags (within the selected section, if any)
        tag_query = "SELECT tags FROM questions"
        tag_params = ()
        if section:
            tag_query += " WHERE section = ?"
            tag_params = (section,)
        tag_cur = conn.execute(tag_query, tag_params)
        tags_all = []
        for row in tag_cur.fetchall():
            if row[0]:
                tags_all.extend([t.strip() for t in row[0].split(",") if t.strip()])
        tags_unique = sorted(set(tags_all))
        tags_hierarchy = build_tag_hierarchy(tags_unique)

    return templates.TemplateResponse(
        "quiz.html",
        {
            "request": request,
            "questions": questions,
            "tags_hierarchy": tags_hierarchy,
            "selected_tag": tag or "",
            "selected_section": section or "",
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
        return RedirectResponse(url="/quiz", status_code=303)
    placeholders = ",".join("?" for _ in answers)
    with get_db() as conn:
        cur = conn.execute(
            f"SELECT id, correct_option FROM questions WHERE id IN ({placeholders})",
            list(answers.keys()),
        )
        result_set = cur.fetchall()
    total = len(result_set)
    correct = 0
    details = []
    for row in result_set:
        qid = str(row[0])
        correct_answer = row["correct_option"]
        user_answer = answers.get(qid)
        is_correct = user_answer == correct_answer
        if is_correct:
            correct += 1
        details.append(
            {
                "id": qid,
                "user_answer": user_answer,
                "correct_answer": correct_answer,
                "is_correct": is_correct,
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
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(
    request: Request,
    msg: Optional[str] = None,
    category: Optional[str] = None,
):
    """Display the admin interface with a list of questions."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM questions ORDER BY id")
        questions = cur.fetchall()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "questions": questions,
            "msg": msg,
            "category": category,
        },
    )


@app.get("/admin/add", response_class=HTMLResponse)
async def add_question_get(request: Request):
    """Render the form to add a new question."""
    return templates.TemplateResponse(
        "add_edit.html",
        {
            "request": request,
            "action": "add",
            "question_data": None,
        },
    )


@app.post("/admin/add", response_class=HTMLResponse)
async def add_question_post(request: Request):
    """Handle submission of a new question."""
    body_bytes = await request.body()
    data = urllib.parse.parse_qs(body_bytes.decode())

    def get_field(field: str) -> str:
        return data.get(field, [""])[0].strip()

    question = get_field("question")
    option_a = get_field("option_a")
    option_b = get_field("option_b")
    option_c = get_field("option_c")
    option_d = get_field("option_d")
    correct_option = get_field("correct_option")
    tags = get_field("tags")
    section = get_field("section")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct_option, tags, section) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                question,
                option_a,
                option_b,
                option_c,
                option_d,
                correct_option,
                tags,
                section,
            ),
        )
        conn.commit()
    # Redirect to admin with success message
    return RedirectResponse(url="/admin?msg=题目已添加&category=success", status_code=303)


@app.get("/admin/edit/{qid}", response_class=HTMLResponse)
async def edit_question_get(request: Request, qid: int):
    """Render the form to edit an existing question."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM questions WHERE id=?", (qid,))
        row = cur.fetchone()
    if row is None:
        return RedirectResponse(
            url="/admin?msg=题目不存在&category=error", status_code=303
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
    return templates.TemplateResponse(
        "add_edit.html",
        {
            "request": request,
            "action": "edit",
            "question_data": question_data,
        },
    )


@app.post("/admin/edit/{qid}", response_class=HTMLResponse)
async def edit_question_post(request: Request, qid: int):
    """Handle submission of edits to an existing question."""
    body_bytes = await request.body()
    data = urllib.parse.parse_qs(body_bytes.decode())

    def get_field(field: str) -> str:
        return data.get(field, [""])[0].strip()

    question = get_field("question")
    option_a = get_field("option_a")
    option_b = get_field("option_b")
    option_c = get_field("option_c")
    option_d = get_field("option_d")
    correct_option = get_field("correct_option")
    tags = get_field("tags")
    section = get_field("section")
    with get_db() as conn:
        conn.execute(
            "UPDATE questions SET question=?, option_a=?, option_b=?, option_c=?, option_d=?, correct_option=?, tags=?, section=? WHERE id=?",
            (
                question,
                option_a,
                option_b,
                option_c,
                option_d,
                correct_option,
                tags,
                section,
                qid,
            ),
        )
        conn.commit()
    return RedirectResponse(url="/admin?msg=题目已更新&category=success", status_code=303)


@app.post("/admin/delete/{qid}", response_class=HTMLResponse)
async def delete_question(request: Request, qid: int):
    """Delete a question from the database."""
    with get_db() as conn:
        conn.execute("DELETE FROM questions WHERE id=?", (qid,))
        conn.commit()
    return RedirectResponse(url="/admin?msg=题目已删除&category=success", status_code=303)
