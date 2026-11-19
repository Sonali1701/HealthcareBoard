-- Run each CREATE INDEX CONCURRENTLY outside a transaction. The indexed
-- expressions must match app/routers/recovered_data.py exactly.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_new_provider_data_search_trgm
ON public."New_provider" USING gin ((
  coalesce(first_name,'') || ' ' || coalesce(last_name,'') || ' ' ||
  coalesce(specialty,'') || ' ' || coalesce(job_category,'') || ' ' ||
  coalesce(credential,'') || ' ' || coalesce(city,'') || ' ' ||
  coalesce(npi_number,'')
) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_new_provider_license_data_search_trgm
ON public."New_provider_license" USING gin ((
  coalesce(full_name,'') || ' ' || coalesce(first_name,'') || ' ' ||
  coalesce(last_name,'') || ' ' || coalesce(specialty,'') || ' ' ||
  coalesce(job_category,'') || ' ' || coalesce(profession,'') || ' ' ||
  coalesce(city,'')
) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_new_provider_data_zip
ON public."New_provider" (zip_code);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_new_provider_license_data_zip
ON public."New_provider_license" (zip_code);
