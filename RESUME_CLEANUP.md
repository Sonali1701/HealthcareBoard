# Data tab resume cleanup

`healthboard-resume-cleanup` is a Render cron job defined in `render.yaml`. It
screens rows in `neondb.public."New_provider"` in UUID order. Its checkpoint,
flagged record IDs, and correction audit all live in Neon, so a new run resumes
after the last committed batch. It does not depend on a laptop or a persistent
Render disk.

## Activate on Render

Sync the updated Blueprint or create a Docker cron service from this repository
with the command `python -m scripts.run_resume_cleanup_cron` and schedule
`*/5 * * * *` (UTC). Set `DATABASE_URL` on this cron service to the **neondb**
connection string. The script refuses to run against any other database.
After the first deploy, trigger a run in the Render dashboard to start at once.
Deploying the web service alone does not create the cron service.

## What it changes

- Corrects a credential stored as a surname only when the first line of a
  parsed source document confirms the first name, surname, and healthcare
  credential, and the credential agrees with the existing category.
- Stores all unresolved screening flags by provider ID for later review.
- Never deletes, reveals, or hides a profile, and never invents missing fields.

The job processes up to 50,000 rows per run in 500-row transactions. Render
ensures only one run of this cron service runs at a time. If interrupted, its
Neon checkpoint resumes from the last committed transaction.

## Check progress in Neon SQL Editor

```sql
SELECT job_name, scanned, corrected, flagged, completed_at, updated_at
FROM public."New_provider_resume_cleanup_progress";

SELECT flag, count(*) AS records
FROM public."New_provider_resume_screen" AS s,
     LATERAL jsonb_array_elements_text(s.flags) AS flag
GROUP BY flag ORDER BY records DESC;
```

`flagged` counts records with at least one issue; the per-flag counts can
overlap. A completed scan is a screening pass, not proof that every résumé is
correct. Records without clear source evidence remain flagged for review.
