"""Indexed radius condition for stored coordinates and existing location data."""
from __future__ import annotations

from sqlalchemy import and_, any_, column, func, or_, select, table, tuple_, union_all

from ..models.profile import Profile


ZIP_CENTROIDS = table("zip_centroids", column("zip"), column("lat"), column("lng"))
CITY_CENTROIDS = table("city_centroids", column("city_lower"), column("state_code"),
                       column("lat"), column("lng"))


def radius_condition(latitude: float, longitude: float, radius_m: float):
    """Include ZIP/city centroids while profile coordinate backfill runs.

    Exact stored coordinates win. For profiles without them, a valid ZIP wins
    over city/state, which is used only when no ZIP centroid exists. PostgreSQL
    can use the normalized ZIP and city/state expression indexes for each arm.
    """
    center = func.ll_to_earth(latitude, longitude)
    bounds = func.earth_box(center, radius_m)

    def nearby(lat, lng):
        point = func.ll_to_earth(lat, lng)
        return and_(bounds.op("@>")(point),
                    func.earth_distance(center, point) <= radius_m)

    stored_profiles = Profile.__table__.alias("geo_stored")
    zip_profiles = Profile.__table__.alias("geo_zip")
    city_profiles = Profile.__table__.alias("geo_city")

    stored_ids = select(stored_profiles.c.profile_id).where(
        stored_profiles.c.lat.isnot(None), stored_profiles.c.lng.isnot(None),
        nearby(stored_profiles.c.lat, stored_profiles.c.lng))

    zip5 = func.substr(func.regexp_replace(
        zip_profiles.c.zip_code, "[^0-9]", "", "g"), 1, 5)
    nearby_zips = select(ZIP_CENTROIDS.c.zip).where(
        nearby(ZIP_CENTROIDS.c.lat, ZIP_CENTROIDS.c.lng))
    zip_ids = select(zip_profiles.c.profile_id).where(
        or_(zip_profiles.c.lat.is_(None), zip_profiles.c.lng.is_(None)),
        zip_profiles.c.zip_code.isnot(None),
        zip5 == any_(func.array(nearby_zips.scalar_subquery())))

    nearby_cities = select(CITY_CENTROIDS.c.city_lower,
                           CITY_CENTROIDS.c.state_code).where(
        nearby(CITY_CENTROIDS.c.lat, CITY_CENTROIDS.c.lng))
    city_zip5 = func.substr(func.regexp_replace(
        city_profiles.c.zip_code, "[^0-9]", "", "g"), 1, 5)
    city_key = tuple_(func.lower(func.btrim(city_profiles.c.city)),
                      func.upper(func.btrim(city_profiles.c.state_code)))
    city_ids = select(city_profiles.c.profile_id).where(
        or_(city_profiles.c.lat.is_(None), city_profiles.c.lng.is_(None)),
        city_profiles.c.city.isnot(None), city_profiles.c.state_code.isnot(None),
        or_(city_profiles.c.zip_code.is_(None),
            ~city_zip5.in_(select(ZIP_CENTROIDS.c.zip))),
        city_key.in_(nearby_cities))
    return Profile.profile_id.in_(union_all(stored_ids, zip_ids, city_ids))


def radius_branches(latitude: float, longitude: float, radius_m: float):
    """Disjoint, indexable predicates for direct profile count/page queries."""
    center = func.ll_to_earth(latitude, longitude)
    bounds = func.earth_box(center, radius_m)

    def nearby(lat, lng):
        point = func.ll_to_earth(lat, lng)
        return and_(bounds.op("@>")(point),
                    func.earth_distance(center, point) <= radius_m)

    missing = or_(Profile.lat.is_(None), Profile.lng.is_(None))
    zip5 = func.substr(func.regexp_replace(
        Profile.zip_code, "[^0-9]", "", "g"), 1, 5)
    nearby_zips = select(ZIP_CENTROIDS.c.zip).where(
        nearby(ZIP_CENTROIDS.c.lat, ZIP_CENTROIDS.c.lng))
    nearby_cities = select(CITY_CENTROIDS.c.city_lower,
                           CITY_CENTROIDS.c.state_code).where(
        nearby(CITY_CENTROIDS.c.lat, CITY_CENTROIDS.c.lng))
    city_key = tuple_(func.lower(func.btrim(Profile.city)),
                      func.upper(func.btrim(Profile.state_code)))
    return (
        and_(Profile.lat.isnot(None), Profile.lng.isnot(None),
             nearby(Profile.lat, Profile.lng)),
        and_(missing, Profile.zip_code.isnot(None),
             zip5 == any_(func.array(nearby_zips.scalar_subquery()))),
        and_(missing, Profile.city.isnot(None), Profile.state_code.isnot(None),
             or_(Profile.zip_code.is_(None),
                 ~zip5.in_(select(ZIP_CENTROIDS.c.zip))),
             city_key.in_(nearby_cities)),
    )
