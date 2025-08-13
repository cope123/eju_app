import sqlite3
from flask import Flask, render_template, request, redirect, url_for, flash, g

app = Flask(__name__)
app.secret_key = "your-secret-key"  # Needed for flash messages

# Path to the SQLite database. It lives in the same folder as the app file for simplicity.
DATABASE = "questions.db"


def get_db():
    """Open a new database connection if there is none yet for the
    current application context. The connection uses row factory
    so that results behave like dictionaries (access columns by name).
    """
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    """Close the database connection at the end of the request."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    """Initialize the database with the questions table."""
    with app.app_context():
        db = get_db()
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL,
                tags TEXT
            );
            """
        )
        db.commit()


@app.before_first_request
def initialize_database():
    """Runs before the first request to ensure the database is ready."""
    init_db()


@app.route('/')
def index():
    """Redirect the root to the quiz page."""
    return redirect(url_for('quiz'))


@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    """Serve the quiz page or process submitted answers.

    On GET: display all questions, optionally filtered by tag.
    On POST: evaluate answers and display the result.
    """
    db = get_db()
    # When the user submits answers
    if request.method == 'POST':
        # form keys are in the form "question-{id}" with value one of 'A','B','C','D'
        answers = {key.split('-')[1]: value for key, value in request.form.items() if key.startswith('question-')}
        # Fetch the relevant questions to check answers
        placeholders = ','.join('?' for _ in answers)
        query = f"SELECT id, correct_option FROM questions WHERE id IN ({placeholders})"
        cur = db.execute(query, list(answers.keys()))
        result_set = cur.fetchall()
        total = len(result_set)
        correct = 0
        detailed_results = []
        # Evaluate each question's answer
        for row in result_set:
            qid = str(row['id'])
            correct_answer = row['correct_option']
            user_answer = answers.get(qid)
            is_correct = (user_answer == correct_answer)
            if is_correct:
                correct += 1
            detailed_results.append({'id': qid, 'user_answer': user_answer, 'correct_answer': correct_answer, 'is_correct': is_correct})
        score_percent = 0
        if total > 0:
            score_percent = round(correct / total * 100, 2)
        return render_template('result.html', total=total, correct=correct, score=score_percent, details=detailed_results)

    # GET method: display the quiz
    tag_filter = request.args.get('tag', '').strip()
    if tag_filter:
        # Filter questions by tag (case-insensitive match among comma-separated tags)
        # Use LIKE with wildcards to find substring match
        like_pattern = f"%{tag_filter}%"
        cur = db.execute("SELECT * FROM questions WHERE lower(tags) LIKE lower(?) ORDER BY id", (like_pattern,))
    else:
        cur = db.execute("SELECT * FROM questions ORDER BY id")
    questions = cur.fetchall()
    # Retrieve list of distinct tags to populate tag filter list
    tag_cur = db.execute("SELECT tags FROM questions")
    tags_all = []
    for row in tag_cur.fetchall():
        if row['tags']:
            # split by comma and strip whitespace
            tags_all.extend([t.strip() for t in row['tags'].split(',') if t.strip()])
    tags_unique = sorted(set(tags_all))
    return render_template('quiz.html', questions=questions, tags=tags_unique, selected_tag=tag_filter)


@app.route('/admin')
def admin():
    """List all questions with options to edit or delete."""
    db = get_db()
    cur = db.execute("SELECT * FROM questions ORDER BY id")
    questions = cur.fetchall()
    return render_template('admin.html', questions=questions)


@app.route('/admin/add', methods=['GET', 'POST'])
def add_question():
    """Add a new multiple‑choice question."""
    if request.method == 'POST':
        question = request.form.get('question', '').strip()
        option_a = request.form.get('option_a', '').strip()
        option_b = request.form.get('option_b', '').strip()
        option_c = request.form.get('option_c', '').strip()
        option_d = request.form.get('option_d', '').strip()
        correct_option = request.form.get('correct_option', '').strip()
        tags = request.form.get('tags', '').strip()
        if not (question and option_a and option_b and option_c and option_d and correct_option):
            flash('请填写所有字段。', 'error')
        else:
            db = get_db()
            db.execute(
                "INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct_option, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (question, option_a, option_b, option_c, option_d, correct_option, tags)
            )
            db.commit()
            flash('题目已添加。', 'success')
            return redirect(url_for('admin'))
    # GET method returns empty form
    return render_template('add_edit.html', action='add', question_data=None)


@app.route('/admin/edit/<int:qid>', methods=['GET', 'POST'])
def edit_question(qid):
    """Edit an existing question specified by ID."""
    db = get_db()
    cur = db.execute("SELECT * FROM questions WHERE id = ?", (qid,))
    question_row = cur.fetchone()
    if not question_row:
        flash('题目不存在。', 'error')
        return redirect(url_for('admin'))
    if request.method == 'POST':
        question = request.form.get('question', '').strip()
        option_a = request.form.get('option_a', '').strip()
        option_b = request.form.get('option_b', '').strip()
        option_c = request.form.get('option_c', '').strip()
        option_d = request.form.get('option_d', '').strip()
        correct_option = request.form.get('correct_option', '').strip()
        tags = request.form.get('tags', '').strip()
        if not (question and option_a and option_b and option_c and option_d and correct_option):
            flash('请填写所有字段。', 'error')
        else:
            db.execute(
                "UPDATE questions SET question=?, option_a=?, option_b=?, option_c=?, option_d=?, correct_option=?, tags=? WHERE id=?",
                (question, option_a, option_b, option_c, option_d, correct_option, tags, qid)
            )
            db.commit()
            flash('题目已更新。', 'success')
            return redirect(url_for('admin'))
    # GET method pre-fills the form with existing data
    question_data = {
        'id': question_row['id'],
        'question': question_row['question'],
        'option_a': question_row['option_a'],
        'option_b': question_row['option_b'],
        'option_c': question_row['option_c'],
        'option_d': question_row['option_d'],
        'correct_option': question_row['correct_option'],
        'tags': question_row['tags'] or ''
    }
    return render_template('add_edit.html', action='edit', question_data=question_data)


@app.route('/admin/delete/<int:qid>', methods=['POST'])
def delete_question(qid):
    """Delete a question by ID."""
    db = get_db()
    db.execute("DELETE FROM questions WHERE id=?", (qid,))
    db.commit()
    flash('题目已删除。', 'success')
    return redirect(url_for('admin'))


if __name__ == '__main__':
    # When running this script directly, start the Flask development server.
    app.run(host='0.0.0.0', port=5000, debug=True)