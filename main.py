"""
Montreal Climate Zone Lookup — DOE Reference Area Scaling Model
--------------------------------------------------------------
✔ Uses fixed DOE medium office baseline = 4982 m²
✔ Three building vintages: New (<22yr), Medium (22-45yr), Old (>45yr)
✔ Normalizes Heating/Cooling/CO2 by reference area
✔ Scales to user input area
✔ Parses Hydro-Québec CSV bills
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import io
import os

app = FastAPI(title="Montreal Climate Energy API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EXCEL_PATH     = "dataset.xlsx"
TARGET_TYPE    = "yes"
REFERENCE_AREA = 4982   # DOE medium office baseline (m²)

# ── Building vintage energy data (per m² of reference building) ──────────────
# Source: DOE EnergyPlus Medium Office Reference Building, Climate Zone 6A
# MJ converted to kWh (÷3.6), values ÷ 4982 m²

VINTAGES = {
    "new": {                        # < 22 years old
        "label": "New construction (<22 years)",
        "heating_kwh_m2": 37.8248,  # from New.xlsx rows 130+146 → kWh/m²
        "cooling_kwh_m2": 13.9703,  # from New.xlsx row 131 → kWh/m²
        "co2_kg_m2":      187.4941, # from New.xlsx row 231 ÷ 4982
    },
    "medium": {                     # 22–45 years old
        "label": "Existing post-1980 (22–45 years)",
        "heating_kwh_m2": 55.9995,  # from Medium.xlsx rows 130+146 → kWh/m²
        "cooling_kwh_m2": 17.4120,  # from Medium.xlsx row 131 → kWh/m²
        "co2_kg_m2":      240.3794, # from Medium.xlsx row 231 ÷ 4982
    },
    "old": {                        # > 45 years old
        "label": "Existing pre-1980 (>45 years)",
        "heating_kwh_m2": 44.6837,  # from Old.xlsx row 152 (MJ→kWh) ÷ 4982
        "cooling_kwh_m2": 14.0768,  # from Old.xlsx row 137 (kWh) ÷ 4982
        "co2_kg_m2":      227.8603, # from Old.xlsx row 303 ÷ 4982
    },
}

df_buildings = None


@app.on_event("startup")
def load_dataset():
    global df_buildings

    if not os.path.exists(EXCEL_PATH):
        print("Dataset not found")
        return

    df = pd.read_excel(EXCEL_PATH)
    df.columns = [c.strip() for c in df.columns]

    df["postal_code"]   = df["postal_code"].astype(str).str.strip().str.upper()
    df["building_type"] = df["building_type"].astype(str).str.strip().str.lower()

    df["footprint_area_m2"] = pd.to_numeric(df["footprint_area_m2"], errors="coerce")
    df["Heating"]           = pd.to_numeric(df["Heating"],           errors="coerce")
    df["Cooling"]           = pd.to_numeric(df["Cooling"],           errors="coerce")

    df = df[~df["building_type"].isin(["no", "true", "false", "nan", "none", ""])]

    df_buildings = df
    print(f"Dataset loaded: {len(df)} rows")


# ─────────────────────────────────────────────
class LookupRequest(BaseModel):
    postal_code:      str
    footprint_area_m2: float
    building_age:     float = 0   # years — used to select vintage


# ─────────────────────────────────────────────
@app.post("/lookup")
def lookup_building(req: LookupRequest):

    if df_buildings is None:
        raise HTTPException(503, "Dataset not loaded")

    code = req.postal_code.strip().upper()[:3]

    # ── Postal prefix match with fallback ──
    in_postal = df_buildings[df_buildings["postal_code"].str.startswith(code)]
    if in_postal.empty:
        in_postal = df_buildings[df_buildings["postal_code"].str.startswith(code[:2])]
    if in_postal.empty:
        in_postal = df_buildings[df_buildings["postal_code"].str.startswith(code[:1])]
    if in_postal.empty:
        raise HTTPException(404, f"No data found for postal code {code}")

    # ── Filter for office buildings ──
    buildings = in_postal[in_postal["building_type"] == TARGET_TYPE].copy()
    if buildings.empty:
        buildings = in_postal.copy()

    buildings["_diff"] = (buildings["footprint_area_m2"] - req.footprint_area_m2).abs()
    b = buildings.sort_values("_diff").iloc[0]

    # ── Select vintage based on building age ──
    age = req.building_age
    if age > 45:
        vintage_key = "old"
    elif age >= 22:
        vintage_key = "medium"
    else:
        vintage_key = "new"

    v = VINTAGES[vintage_key]
    user_area = req.footprint_area_m2

    scaled_heating = v["heating_kwh_m2"] * user_area
    scaled_cooling = v["cooling_kwh_m2"] * user_area
    scaled_co2     = v["co2_kg_m2"]     * user_area

    def safe(val):
        try:
            if pd.isna(val): return None
        except: pass
        if isinstance(val, float) and val.is_integer(): return int(val)
        return val

    return {
        "building": {
            "postal_code":       b["postal_code"],
            "building_type":     b["building_type"],
            "footprint_area_m2": safe(b["footprint_area_m2"]),
            "climate_zone":      safe(b["Climate Zone"]),
        },
        "energy_model": {
            "vintage":            vintage_key,
            "vintage_label":      v["label"],
            "building_age":       age,
            "reference_area_m2":  REFERENCE_AREA,
            "heating_kwh_m2":     round(v["heating_kwh_m2"], 4),
            "cooling_kwh_m2":     round(v["cooling_kwh_m2"], 4),
            "co2_kg_m2":          round(v["co2_kg_m2"], 4),
            "scaled_heating_kwh": round(scaled_heating, 2),
            "scaled_cooling_kwh": round(scaled_cooling, 2),
            "scaled_co2_kg":      round(scaled_co2, 2),
            "user_area_m2":       user_area,
        }
    }


# ─────────────────────────────────────────────
@app.post("/parse-csv")
async def parse_csv(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents), encoding="latin1", sep=";")
        df.columns = [c.strip() for c in df.columns]

        months = []
        for _, row in df.iterrows():
            try:
                kwh    = float(row["kWh"])        if pd.notna(row["kWh"])        else 0
                amount = float(row["Amount ($)"])  if pd.notna(row["Amount ($)"]) else 0
                start  = str(row["Starting date"])[:7]
                months.append({ "month": start, "kwh": round(kwh, 2), "amount": round(amount, 2) })
            except:
                continue

        if not months:
            raise HTTPException(400, "No valid billing data found in CSV")

        total_kwh    = sum(m["kwh"]    for m in months)
        total_amount = sum(m["amount"] for m in months)
        avg_amount   = total_amount / len(months) if months else 0

        return {
            "months":       months,
            "total_kwh":    round(total_kwh, 2),
            "total_amount": round(total_amount, 2),
            "avg_amount":   round(avg_amount, 2),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not parse CSV: {str(e)}")


# ─────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "dataset_loaded": df_buildings is not None}
