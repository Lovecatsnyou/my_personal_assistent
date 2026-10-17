"""SQLite operations for the user's saved weather city (table user_locations).

Independent of user_settings.timezone: changing the weather city never touches reminders.
"""

from database import Database
from weather_service import MAX_NAME_LENGTH, Place, valid_zone


def get_location(db: Database, user_id: int) -> tuple[Place, str] | None:
    """The saved place and when it was saved (UTC, SQLite format)."""
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM user_locations WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    place = Place(
        row["city_name"], row["country"], row["region"], row["latitude"], row["longitude"],
        row["timezone"] if valid_zone(row["timezone"]) else "", row["geocoding_id"],
    )
    return place, row["updated_at"]


def save_location(db: Database, user_id: int, place: Place) -> None:
    name = " ".join(place.name.split())
    if not 0 < len(name) <= MAX_NAME_LENGTH:
        raise ValueError("Некорректное название населённого пункта.")
    if not (-90 <= place.latitude <= 90 and -180 <= place.longitude <= 180):
        raise ValueError("Некорректные координаты.")
    zone = place.timezone if valid_zone(place.timezone) else ""
    with db.connection() as conn:
        conn.execute("""
            INSERT INTO user_locations
                (user_id, city_name, country, region, latitude, longitude, timezone, geocoding_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                city_name = excluded.city_name, country = excluded.country, region = excluded.region,
                latitude = excluded.latitude, longitude = excluded.longitude, timezone = excluded.timezone,
                geocoding_id = excluded.geocoding_id, updated_at = CURRENT_TIMESTAMP
        """, (user_id, name, place.country, place.region, place.latitude, place.longitude, zone,
              place.geocoding_id))
