"""Generate the fictional restaurant dataset as plain CSV files in data/.

Trattoria Verde is a made-up Italian restaurant in Hyderabad. The CSVs are the
"spreadsheet" layer; scripts/seed.py loads them into SQLite.

Allergen data is deliberately imperfect, because that is what the agent has to cope with:

* dishes with allergen_info_status = "unverified" or "missing" have NO allergen rows;
* Pizza Margherita is "verified" but has no row for sesame (a gap in the sheet);
* Focaccia and Calamari carry "may_contain" rows (shared oven / shared fryer).

The agent must answer allergen questions ONLY from verified rows and say it cannot confirm otherwise.

Run:  python scripts/make_dataset.py
"""

from __future__ import annotations

import csv
from pathlib import Path

ALLERGENS = [
    "gluten",
    "crustaceans",
    "eggs",
    "fish",
    "peanuts",
    "tree_nuts",
    "soy",
    "milk",
    "celery",
    "mustard",
    "sesame",
    "sulphites",
    "lupin",
    "molluscs",
]

# id, name, aliases, category, description, price_inr, vegetarian, vegan, ingredients,
# info_status, contains, may_contain, missing_rows, note
DISHES = [
    (
        1,
        "Bruschetta al Pomodoro",
        "bruschetta|tomato bruschetta",
        "antipasti",
        "Grilled sourdough with tomato, garlic and basil",
        320,
        1,
        1,
        "sourdough bread|tomato|garlic|basil|olive oil",
        "verified",
        ["gluten"],
        [],
        [],
        "",
    ),
    (
        2,
        "Pesto Rosso Bruschetta",
        "pesto rosso|red pesto bruschetta|pesto bruschetta",
        "antipasti",
        "Toast with sun-dried tomato pesto and ricotta",
        360,
        1,
        0,
        "sourdough bread|sun-dried tomato pesto|ricotta",
        "unverified",
        [],
        [],
        [],
        "New pesto supplier since November; allergen sheet not yet received",
    ),
    (
        3,
        "Burrata e Pomodorini",
        "burrata",
        "antipasti",
        "Fresh burrata with cherry tomatoes and basil oil",
        520,
        1,
        0,
        "burrata|cherry tomato|basil oil",
        "verified",
        ["milk"],
        [],
        [],
        "",
    ),
    (
        4,
        "Calamari Fritti",
        "calamari|fried calamari|fried squid",
        "antipasti",
        "Crispy fried squid with lemon aioli",
        560,
        0,
        0,
        "squid|flour|egg|lemon aioli",
        "verified",
        ["molluscs", "gluten", "eggs"],
        ["crustaceans"],
        [],
        "Shared fryer with prawns",
    ),
    (
        5,
        "Arancini ai Funghi",
        "arancini|mushroom arancini|rice balls",
        "antipasti",
        "Fried risotto balls with mushroom and mozzarella",
        420,
        1,
        0,
        "arborio rice|mushroom|mozzarella|breadcrumbs|egg",
        "verified",
        ["gluten", "milk", "eggs"],
        [],
        [],
        "",
    ),
    (
        6,
        "Minestrone",
        "minestrone soup|vegetable soup",
        "zuppe",
        "Vegetable soup with ditalini pasta",
        300,
        1,
        1,
        "celery|carrot|beans|ditalini pasta|tomato",
        "verified",
        ["celery", "gluten"],
        [],
        [],
        "",
    ),
    (
        7,
        "Insalata Caprese",
        "caprese|caprese salad",
        "insalate",
        "Tomato, mozzarella, basil",
        450,
        1,
        0,
        "tomato|mozzarella|basil|olive oil",
        "verified",
        ["milk"],
        [],
        [],
        "",
    ),
    (
        8,
        "Caesar Salad",
        "caesar",
        "insalate",
        "Romaine, anchovy dressing, parmesan, croutons",
        480,
        0,
        0,
        "romaine|anchovy|egg yolk|parmesan|croutons|dijon mustard",
        "verified",
        ["fish", "eggs", "milk", "gluten", "mustard"],
        [],
        [],
        "",
    ),
    (
        9,
        "Trofie al Pesto Genovese",
        "pesto|pesto pasta|trofie|pesto genovese|basil pesto",
        "pasta",
        "Hand-rolled trofie with basil pesto, pine nuts and pecorino",
        640,
        1,
        0,
        "trofie pasta|basil|pine nuts|parmigiano|pecorino|garlic|olive oil",
        "verified",
        ["tree_nuts", "milk", "gluten"],
        [],
        [],
        "Pesto made in-house with pine nuts",
    ),
    (
        10,
        "Spaghetti Carbonara",
        "carbonara",
        "pasta",
        "Spaghetti, egg yolk, pecorino, guanciale",
        620,
        0,
        0,
        "spaghetti|egg yolk|pecorino|guanciale|black pepper",
        "verified",
        ["eggs", "milk", "gluten"],
        [],
        [],
        "",
    ),
    (
        11,
        "Penne Arrabbiata",
        "arrabbiata|penne",
        "pasta",
        "Penne in spicy tomato sauce",
        520,
        1,
        1,
        "penne|tomato|chilli|garlic|olive oil",
        "verified",
        ["gluten"],
        [],
        [],
        "",
    ),
    (
        12,
        "Linguine ai Frutti di Mare",
        "seafood linguine|frutti di mare|seafood pasta",
        "pasta",
        "Linguine with prawns, mussels, clams and white wine",
        880,
        0,
        0,
        "linguine|prawns|mussels|clams|white wine|garlic",
        "verified",
        ["crustaceans", "molluscs", "gluten", "sulphites"],
        [],
        [],
        "",
    ),
    (
        13,
        "Lasagna della Nonna",
        "lasagna|lasagne",
        "pasta",
        "Baked lasagna with chicken ragu and bechamel",
        720,
        0,
        0,
        "pasta sheets|chicken ragu|bechamel|parmigiano|celery|egg",
        "verified",
        ["gluten", "milk", "eggs", "celery"],
        [],
        [],
        "",
    ),
    (
        14,
        "Gnocchi Gorgonzola e Noci",
        "gnocchi|gorgonzola gnocchi",
        "pasta",
        "Potato gnocchi with gorgonzola cream and walnuts",
        690,
        1,
        0,
        "potato gnocchi|gorgonzola|cream|walnuts|egg|flour",
        "verified",
        ["tree_nuts", "milk", "gluten", "eggs"],
        [],
        [],
        "",
    ),
    (
        15,
        "Pizza Margherita",
        "margherita|margherita pizza|cheese pizza",
        "pizza",
        "Tomato, fior di latte, basil",
        560,
        1,
        0,
        "pizza dough|tomato|fior di latte|basil",
        "verified",
        ["gluten", "milk"],
        [],
        ["sesame"],
        "Sesame not assessed: shares the oven with the sesame focaccia",
    ),
    (
        16,
        "Pizza Diavola",
        "diavola|pepperoni pizza|spicy pizza",
        "pizza",
        "Tomato, mozzarella, spicy chicken salami",
        660,
        0,
        0,
        "pizza dough|tomato|mozzarella|chicken salami|chilli",
        "verified",
        ["gluten", "milk"],
        [],
        [],
        "",
    ),
    (
        17,
        "Pollo alla Cacciatora",
        "chicken cacciatora|cacciatora|hunter's chicken",
        "secondi",
        "Braised chicken with tomato, olives and white wine",
        780,
        0,
        0,
        "chicken|tomato|olives|white wine|celery|rosemary",
        "verified",
        ["celery", "sulphites"],
        [],
        [],
        "",
    ),
    (
        18,
        "Branzino al Forno",
        "branzino|sea bass|baked fish",
        "secondi",
        "Oven-baked sea bass with lemon and herbs",
        950,
        0,
        0,
        "sea bass|lemon|herbs|olive oil",
        "verified",
        ["fish"],
        [],
        [],
        "",
    ),
    (
        19,
        "Risotto del Giorno",
        "risotto|risotto of the day|chef's risotto|special risotto",
        "secondi",
        "Chef's risotto of the day; ingredients change daily",
        740,
        1,
        0,
        "arborio rice|seasonal ingredients",
        "missing",
        [],
        [],
        [],
        "Changes daily; no allergen sheet",
    ),
    (
        20,
        "Melanzane alla Parmigiana",
        "parmigiana|eggplant parmigiana|aubergine parmigiana",
        "secondi",
        "Baked aubergine, tomato and mozzarella",
        640,
        1,
        0,
        "aubergine|tomato|mozzarella|parmigiana|flour|egg",
        "verified",
        ["milk", "gluten", "eggs"],
        [],
        [],
        "",
    ),
    (
        21,
        "Tiramisu",
        "tiramisu",
        "dolci",
        "Mascarpone, coffee-soaked savoiardi, cocoa",
        420,
        1,
        0,
        "mascarpone|egg|savoiardi|coffee|marsala|cocoa",
        "verified",
        ["eggs", "milk", "gluten", "sulphites"],
        [],
        [],
        "",
    ),
    (
        22,
        "Panna Cotta al Pistacchio",
        "panna cotta|pistachio panna cotta",
        "dolci",
        "Vanilla panna cotta with pistachio crumble",
        400,
        1,
        0,
        "cream|gelatin|vanilla|pistachio",
        "verified",
        ["tree_nuts", "milk"],
        ["peanuts"],
        [],
        "Pistachio crumble made in a kitchen that handles peanuts",
    ),
    (
        23,
        "Torta Caprese",
        "chocolate almond cake|torta caprese|chocolate cake",
        "dolci",
        "Flourless chocolate and almond cake",
        380,
        1,
        0,
        "dark chocolate|almonds|eggs|butter|sugar",
        "verified",
        ["tree_nuts", "eggs", "milk"],
        [],
        [],
        "",
    ),
    (
        24,
        "Sorbetto al Limone",
        "lemon sorbet|sorbet|sorbetto",
        "dolci",
        "Lemon sorbet from an outside supplier",
        300,
        1,
        1,
        "lemon|sugar|water",
        "unverified",
        [],
        [],
        [],
        "Supplier allergen declaration expired; awaiting renewal",
    ),
    (
        25,
        "Focaccia della Casa",
        "focaccia|house bread|bread",
        "pane",
        "Rosemary focaccia with sesame crust",
        250,
        1,
        1,
        "flour|olive oil|rosemary|sesame seeds|salt",
        "verified",
        ["gluten", "sesame"],
        [],
        [],
        "",
    ),
]

HOURS = [  # weekday (0=Mon), open, close, last_seating  (several rows per day = several services)
    (0, None, None, None),
    (1, "12:00", "15:00", "14:00"),
    (1, "18:00", "22:30", "21:30"),
    (2, "12:00", "15:00", "14:00"),
    (2, "18:00", "22:30", "21:30"),
    (3, "12:00", "15:00", "14:00"),
    (3, "18:00", "22:30", "21:30"),
    (4, "12:00", "15:00", "14:00"),
    (4, "18:00", "23:00", "22:00"),
    (5, "12:00", "15:00", "14:00"),
    (5, "18:00", "23:00", "22:00"),
    (6, "12:00", "22:00", "21:00"),
]

TABLES = [  # id, seats, area
    ("T1", 2, "window"),
    ("T2", 2, "window"),
    ("T3", 2, "main"),
    ("T4", 2, "main"),
    ("T5", 4, "main"),
    ("T6", 4, "main"),
    ("T7", 4, "patio"),
    ("T8", 4, "patio"),
    ("T9", 6, "main"),
    ("T10", 6, "patio"),
    ("T11", 8, "private nook"),
]

# Existing bookings, as offsets from the "base date" (the eval clock's day, a Friday).
# day_offset, time, party, table, name, phone, code
RESERVATIONS = [
    # Friday dinner: every 4/6/8-top taken from 21:00, so 4 people at 20:00 does not fit; 19:30 does.
    (0, "21:00", 4, "T5", "Daniel Fernandes", "+919800000101", "TV-D4F8"),
    (0, "21:00", 3, "T6", "Meera Iyer", "+919800000102", "TV-M2I6"),
    (0, "21:00", 4, "T7", "Karan Shah", "+919800000103", "TV-K8S3"),
    (0, "21:00", 4, "T8", "Sara Khan", "+919800000104", "TV-S5K1"),
    (0, "21:00", 6, "T9", "Rohit Verma", "+919800000105", "TV-R9V2"),
    (0, "21:00", 5, "T10", "Lakshmi Nair", "+919800000106", "TV-L3N7"),
    (0, "21:00", 8, "T11", "Office Party Arjun", "+919800000107", "TV-A8P4"),
    (0, "19:00", 2, "T1", "Ananya Rao", "+919800000108", "TV-A7K2"),
    (0, "19:30", 2, "T2", "Imran Qureshi", "+919800000109", "TV-I4Q5"),
    # Saturday dinner: everything taken at 19:00 and 20:30, so only 22:00 is open.
    *[
        (1, "19:00", s, t, f"Sat Guest {t}", f"+91980000{200 + i:04d}", f"TV-SA{i:02d}")
        for i, (t, s, _) in enumerate(TABLES)
    ],
    *[
        (1, "20:30", s, t, f"Sat Late {t}", f"+91980000{300 + i:04d}", f"TV-SB{i:02d}")
        for i, (t, s, _) in enumerate(TABLES)
    ],
    # Sunday lunch, used by modify/cancel scenarios.
    (2, "13:00", 6, "T9", "Vikram Mehta", "+919800000110", "TV-V3M9"),
]


def main(out_dir: Path = Path(__file__).resolve().parents[1] / "data") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "menu.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "id",
                "name",
                "aliases",
                "category",
                "description",
                "price_inr",
                "vegetarian",
                "vegan",
                "ingredients",
                "allergen_info_status",
                "allergen_note",
            ]
        )
        for d in DISHES:
            w.writerow([*d[:9], d[9], d[13]])
    with open(out_dir / "allergens.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dish_id", "allergen", "status", "source", "verified_on"])
        for d in DISHES:
            did, status, contains, may, missing = d[0], d[9], d[10], d[11], d[12]
            if status != "verified":
                continue  # no verified rows at all
            for a in ALLERGENS:
                if a in missing:
                    continue  # deliberate gap
                s = "contains" if a in contains else "may_contain" if a in may else "free"
                w.writerow([did, a, s, "chef allergen sheet v3", "2026-10-28"])
    with open(out_dir / "hours.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["weekday", "open", "close", "last_seating"])
        for h in HOURS:
            w.writerow(["" if v is None else v for v in h])
    with open(out_dir / "tables.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "seats", "area"])
        w.writerows(TABLES)
    with open(out_dir / "reservations.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day_offset", "time", "party_size", "table_id", "name", "phone", "code"])
        w.writerows(RESERVATIONS)
    print(f"wrote dataset to {out_dir}")


if __name__ == "__main__":
    main()
