"""
Investigation: fitness_power_quiz attribution pattern
Run: cd superage-staging && python investigate_fitness_power_quiz.py
"""
import os, psycopg2, sys

os.environ.setdefault("DB_HOST","powerbi.ctqeq4e88wx8.us-west-1.rds.amazonaws.com")
os.environ.setdefault("DB_PORT","5432")
os.environ.setdefault("DB_NAME","postgres")
os.environ.setdefault("DB_USER","postgres")
os.environ.setdefault("DB_PASSWORD","PostgresAdmin1234")

conn = psycopg2.connect(
    host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"], sslmode="require"
)
cur = conn.cursor()

print("=" * 60)
print("1. WHICH FIELD DRIVES 'fitness_power_quiz'?")
print("=" * 60)
cur.execute("""
SELECT
    COUNT(*) FILTER (WHERE LOWER(TRIM(sub_source)) = 'fitness_power_quiz') AS in_sub_source,
    COUNT(*) FILTER (WHERE LOWER(TRIM(source))     = 'fitness_power_quiz') AS in_source,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) = 'fitness_power_quiz') AS in_utm_source
FROM superage.subscribers
""")
r = cur.fetchone()
print(f"  sub_source  : {r[0]:,}")
print(f"  source      : {r[1]:,}")
print(f"  utm_source  : {r[2]:,}")

print()
print("=" * 60)
print("2. BREAKDOWN: utm_source / source / o_event for fitness_power_quiz subs")
print("   (where source = 'fitness_power_quiz')")
print("=" * 60)
cur.execute("""
SELECT
    COALESCE(NULLIF(TRIM(utm_source),''), '(empty)') AS utm_src,
    COALESCE(NULLIF(TRIM(sub_source),''), '(empty)') AS sub_src,
    COALESCE(NULLIF(TRIM(o_event),''), '(empty)')    AS o_ev,
    COUNT(*) AS cnt
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
GROUP BY 1,2,3
ORDER BY cnt DESC
LIMIT 30
""")
rows = cur.fetchall()
print(f"  {'utm_source':<25} {'sub_source':<25} {'o_event':<25} {'count':>10}")
print(f"  {'-'*25} {'-'*25} {'-'*25} {'-'*10}")
for r in rows:
    print(f"  {r[0]:<25} {r[1]:<25} {r[2]:<25} {r[3]:>10,}")

print()
print("=" * 60)
print("3. MONTHLY TREND — fitness_power_quiz subs by month (last 18 months)")
print("=" * 60)
cur.execute("""
SELECT
    TO_CHAR(DATE_TRUNC('month', date_joined), 'YYYY-MM') AS month,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')) AS utm_is_meta,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) = 'fitness_power_quiz')           AS utm_is_fpq,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) = '')                             AS utm_empty,
    COUNT(*) FILTER (WHERE utm_source IS NULL)                                       AS utm_null
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
  AND date_joined >= NOW() - INTERVAL '18 months'
GROUP BY 1
ORDER BY 1 DESC
""")
rows = cur.fetchall()
print(f"  {'Month':<10} {'Total':>8} {'utm=Meta':>10} {'utm=fpq':>10} {'utm=empty':>10} {'utm=null':>10}")
print(f"  {'-'*10} {'-'*8} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
for r in rows:
    print(f"  {r[0]:<10} {r[1]:>8,} {r[2]:>10,} {r[3]:>10,} {r[4]:>10,} {r[5]:>10,}")

print()
print("=" * 60)
print("4. ACQUISITION TABLE — what acquisition_utm_source says for these subs")
print("=" * 60)
cur.execute("""
SELECT
    COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''), '(empty/null)') AS acq_utm,
    COUNT(*) AS cnt
FROM superage.subscribers s
JOIN superage.subscriber_acquisition sa
    ON LOWER(TRIM(s.email)) = LOWER(TRIM(sa.email))
WHERE LOWER(TRIM(s.source)) = 'fitness_power_quiz'
GROUP BY 1
ORDER BY cnt DESC
LIMIT 20
""")
rows = cur.fetchall()
print(f"  {'acquisition_utm_source':<40} {'count':>10}")
print(f"  {'-'*40} {'-'*10}")
for r in rows:
    print(f"  {r[0]:<40} {r[1]:>10,}")

print()
print("=" * 60)
print("5. SAMPLE ROWS — 20 recent fitness_power_quiz subs (last 60 days)")
print("=" * 60)
cur.execute("""
SELECT
    LEFT(email,4)||'***'       AS email_mask,
    TO_CHAR(date_joined,'YYYY-MM-DD') AS joined,
    COALESCE(NULLIF(TRIM(utm_source),''),'(empty)') AS utm_source,
    COALESCE(NULLIF(TRIM(sub_source),''),'(empty)') AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(empty)')     AS source,
    COALESCE(NULLIF(TRIM(o_event),''),'(empty)')    AS o_event
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
  AND date_joined >= NOW() - INTERVAL '60 days'
ORDER BY date_joined DESC
LIMIT 20
""")
rows = cur.fetchall()
if rows:
    print(f"  {'email':<12} {'joined':<12} {'utm_source':<20} {'sub_source':<20} {'source':<20} {'o_event':<20}")
    print(f"  {'-'*12} {'-'*12} {'-'*20} {'-'*20} {'-'*20} {'-'*20}")
    for r in rows:
        print(f"  {r[0]:<12} {r[1]:<12} {r[2]:<20} {r[3]:<20} {r[4]:<20} {r[5]:<20}")
else:
    print("  No fitness_power_quiz subs in last 60 days")

print()
print("=" * 60)
print("6. O_EVENT DISTRIBUTION across ALL fitness_power_quiz subs")
print("=" * 60)
cur.execute("""
SELECT
    COALESCE(NULLIF(TRIM(o_event),''), '(empty)') AS o_ev,
    COUNT(*) AS cnt,
    MIN(date_joined::date) AS first_seen,
    MAX(date_joined::date) AS last_seen
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
GROUP BY 1
ORDER BY cnt DESC
""")
rows = cur.fetchall()
print(f"  {'o_event':<30} {'count':>10} {'first_seen':<12} {'last_seen':<12}")
print(f"  {'-'*30} {'-'*10} {'-'*12} {'-'*12}")
for r in rows:
    print(f"  {r[0]:<30} {r[1]:>10,} {str(r[2]):<12} {str(r[3]):<12}")

cur.close(); conn.close()
print("\n✓ Done.")
