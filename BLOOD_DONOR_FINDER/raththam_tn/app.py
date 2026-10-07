import os, json
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, session, flash, g, url_for

app = Flask(__name__)
app.secret_key = "raththam-tn-secret"
# ---- PostgreSQL settings: put YOUR PostgreSQL password below ----
DB_CONFIG = dict(host="localhost", port=5432, dbname="lifelink", user="postgres",
                 password=os.environ.get("PGPASSWORD", "postgresql"))

GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-",
          "A1+", "A1-", "A2+", "A2-", "A1B+", "A1B-", "A2B+", "A2B-", "Bombay"]
# A main group also covers its subgroups (a request for A+ can be met by A1+ or A2+ donors)
SUBGROUPS = {"A+": ["A1+", "A2+"], "A-": ["A1-", "A2-"], "AB+": ["A1B+", "A2B+"], "AB-": ["A1B-", "A2B-"]}
# All 38 districts of Tamil Nadu with their neighbouring districts (for "include nearby districts")
with open(os.path.join(os.path.dirname(__file__), "near.json"), encoding="utf-8") as f:
    NEAR = json.load(f)
DISTRICTS = sorted(NEAR)

def matching(bg):
    return [bg] + SUBGROUPS.get(bg, [])

class DB:
    """Small wrapper so db().execute(sql, args).fetchall() works."""
    def __init__(self):
        url = os.environ.get("DATABASE_URL")
        self.con = psycopg2.connect(url) if url else psycopg2.connect(**DB_CONFIG)
    def execute(self, sql, args=()):
        cur = self.con.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, args)
        return cur
    def commit(self): self.con.commit()

def db():
    if "db" not in g:
        g.db = DB()
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.con.close()

@app.context_processor
def inject():
    return dict(DISTRICTS=DISTRICTS)

def is_admin(): return session.get("admin")

def clean_district(v):
    return v if v in NEAR else None

@app.route("/")
def home():
    d = db()
    counts = {r["blood_group"]: r["n"] for r in d.execute(
        "SELECT blood_group, COUNT(*) n FROM available_donors GROUP BY blood_group")}
    totals = d.execute("""SELECT (SELECT COUNT(*) FROM donor) donors,
        (SELECT COUNT(*) FROM available_donors) available,
        (SELECT COUNT(DISTINCT district) FROM donor) districts,
        (SELECT COUNT(*) FROM hospital) hospitals""").fetchone()
    sos = d.execute("""SELECT r.request_id, r.urgency, r.units_needed,
        (SELECT COALESCE(SUM(units),0) FROM donation WHERE request_id = r.request_id) AS received, p.name, p.blood_group, p.district, p.phone, h.name hospital
        FROM request r JOIN patient p USING(patient_id) JOIN hospital h USING(hospital_id)
        WHERE r.status='Pending' ORDER BY CASE r.urgency WHEN 'Emergency' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END,
        r.request_id DESC LIMIT 4""").fetchall()
    heroes = d.execute("""SELECT name, blood_group, district, total_donations FROM donor
        ORDER BY total_donations DESC, name LIMIT 5""").fetchall()
    return render_template("home.html", groups=GROUPS, counts=counts, totals=totals, sos=sos, heroes=heroes)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        dist = clean_district(f["district"])
        if not dist:
            flash("Please choose a district.")
            return redirect(url_for("register"))
        db().execute("""INSERT INTO donor(name,age,gender,blood_group,phone,district,last_donation_date,total_donations)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                     (f["name"], f["age"], f["gender"], f["blood_group"], f["phone"], dist,
                      f["last_donation_date"] or None, 1 if f["last_donation_date"] else 0))
        db().commit()
        flash("Thank you! You are now part of the RaththamTN donor family.")
        return redirect(url_for("search", district=dist))
    return render_template("register.html", groups=GROUPS)

@app.route("/search")
def search():
    bg = request.args.get("bg", "")
    district = request.args.get("district", "")
    nearby = request.args.get("nearby") == "1"
    area = [district] + NEAR[district] if (district in NEAR and nearby) else ([district] if district in NEAR else [])
    sql, args = "SELECT * FROM available_donors WHERE 1=1", []
    if bg: sql += " AND blood_group = ANY(%s)"; args.append(matching(bg))
    if area: sql += " AND district = ANY(%s)"; args.append(area)
    sql += " ORDER BY (district=%s) DESC, total_donations DESC, name"; args.append(district)
    d = db()
    donors = d.execute(sql, args).fetchall()
    waiting, stock = [], []
    if bg:
        waiting = d.execute("""SELECT *, (last_donation_date + 90) AS eligible_from FROM donor
            WHERE blood_group = ANY(%s) AND last_donation_date > CURRENT_DATE - 90
            AND (cardinality(%s::text[])=0 OR district = ANY(%s))
            ORDER BY last_donation_date""", (matching(bg), area, area)).fetchall()
        stock = d.execute("""SELECT h.name, h.district, h.phone, s.units FROM stock s
            JOIN hospital h USING(hospital_id) WHERE s.blood_group = ANY(%s) AND s.units>0
            AND (cardinality(%s::text[])=0 OR h.district = ANY(%s)) ORDER BY s.units DESC LIMIT 12""",
            (matching(bg), area, area)).fetchall()
    return render_template("search.html", donors=donors, groups=GROUPS, bg=bg, district=district,
                           nearby=nearby, waiting=waiting, stock=stock, area=area)

@app.route("/request", methods=["GET", "POST"])
def new_request():
    d = db()
    if request.method == "POST":
        f = request.form
        dist = clean_district(f["district"])
        if not dist:
            flash("Please choose a district.")
            return redirect(url_for("new_request"))
        cur = d.execute("INSERT INTO patient(name,age,blood_group,hospital_id,district,phone) VALUES(%s,%s,%s,%s,%s,%s) RETURNING patient_id",
                        (f["name"], f["age"], f["blood_group"], f["hospital_id"], dist, f["phone"]))
        try:
            units = max(1, min(int(f.get("units_needed", 1)), 10))
        except ValueError:
            units = 1
        d.execute("INSERT INTO request(patient_id,urgency,units_needed) VALUES(%s,%s,%s)",
                  (cur.fetchone()["patient_id"], f["urgency"], units))
        d.commit()
        flash("Request sent. Matching donors in your district and nearby districts are shown below.")
        return redirect(url_for("search", bg=f["blood_group"], district=dist, nearby=1))
    return render_template("request.html", groups=GROUPS,
                           hospitals=d.execute("SELECT * FROM hospital ORDER BY district, name").fetchall())

@app.route("/requests")
def requests_list():
    rows = db().execute("""SELECT r.request_id, r.urgency, r.request_date, r.status, r.units_needed,
        (SELECT COALESCE(SUM(units),0) FROM donation WHERE request_id = r.request_id) AS received, p.name, p.blood_group, p.district, p.phone, h.name hospital
        FROM request r JOIN patient p USING(patient_id) JOIN hospital h USING(hospital_id)
        ORDER BY CASE r.urgency WHEN 'Emergency' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END, r.request_id DESC""").fetchall()
    return render_template("requests.html", rows=rows)

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT 1 FROM admin WHERE username=%s AND password=%s",
                         (request.form["username"], request.form["password"])).fetchone()
        if u:
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Wrong username or password.")
    return render_template("login.html")

@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/admin")
def admin():
    if not is_admin(): return redirect(url_for("login"))
    d = db()
    return render_template("admin.html",
        by_group=d.execute("SELECT blood_group, COUNT(*) n FROM donor GROUP BY blood_group ORDER BY n DESC").fetchall(),
        by_district=d.execute("SELECT district, COUNT(*) n FROM donor GROUP BY district ORDER BY n DESC LIMIT 10").fetchall(),
        pending=d.execute("""SELECT r.request_id, r.urgency, r.units_needed,
        (SELECT COALESCE(SUM(units),0) FROM donation WHERE request_id = r.request_id) AS received, p.name, p.blood_group, p.district FROM request r
            JOIN patient p USING(patient_id) WHERE r.status='Pending'
            ORDER BY CASE r.urgency WHEN 'Emergency' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END""").fetchall(),
        stock=d.execute("""SELECT blood_group, SUM(units) units FROM stock GROUP BY blood_group ORDER BY units""").fetchall(),
        low=d.execute("""SELECT h.name, h.district, s.blood_group, s.units FROM stock s JOIN hospital h USING(hospital_id)
            WHERE s.units <= 1 AND s.blood_group IN ('O-','B-','A-','AB-') ORDER BY s.units, h.name LIMIT 8""").fetchall(),
        totals=d.execute("""SELECT (SELECT COUNT(*) FROM donor) donors, (SELECT COUNT(*) FROM available_donors) available,
            (SELECT COUNT(*) FROM request WHERE status='Pending') pending,
            (SELECT COALESCE(SUM(units),0) FROM donation) units""").fetchone())

@app.route("/admin/donate/<int:rid>", methods=["GET", "POST"])
def donate(rid):
    if not is_admin(): return redirect(url_for("login"))
    d = db()
    req = d.execute("SELECT r.*, p.name, p.blood_group, p.district, p.hospital_id FROM request r JOIN patient p USING(patient_id) WHERE r.request_id=%s", (rid,)).fetchone()
    if request.method == "POST":
        d.execute("INSERT INTO donation(donor_id,request_id,hospital_id,units) VALUES(%s,%s,%s,%s)",
                  (request.form["donor_id"], rid, req["hospital_id"], request.form["units"]))
        got = d.execute("SELECT COALESCE(SUM(units),0) n FROM donation WHERE request_id=%s", (rid,)).fetchone()["n"]
        if got >= req["units_needed"]:
            d.execute("UPDATE request SET status='Fulfilled' WHERE request_id=%s", (rid,))
            flash("Request fulfilled. Stock, donor date and donation count were updated by the trigger.")
        else:
            flash(f"Donation recorded. {req['units_needed'] - got} more unit(s) still needed.")
        d.commit()
        return redirect(url_for("admin"))
    donors = d.execute("""SELECT * FROM available_donors WHERE blood_group = ANY(%s)
        AND district = ANY(%s) ORDER BY (district=%s) DESC, name""",
        (matching(req["blood_group"]), [req["district"]] + NEAR[req["district"]], req["district"])).fetchall()
    return render_template("donate.html", req=req, donors=donors)

if __name__ == "__main__":
    app.run(debug=True)
