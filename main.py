"""
Montreal Climate Zone Lookup — DOE Reference Area Scaling Model
--------------------------------------------------------------
Source: DOE EnergyPlus Medium Office Reference Building, Climate Zone 5A
Three vintages: New (<22yr), Medium (22-45yr), Old (>45yr)
Heating = Electricity heating (kWh) + Gas heating (MJ ÷ 3.6)
Cooling = Electricity cooling (kWh) only
All values divided by 5000 m² reference area, then scaled to user area
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
REFERENCE_AREA = 5000   # as specified

# ── Per-m² values from DOE EnergyPlus raw data ÷ 5000 ──────────────────────
# OLD   (>45yr):  Heating = 0 + 801410 MJ÷3.6 = 222614 kWh ÷ 5000
#                 Cooling = 70131 kWh ÷ 5000
#                 CO2     = 1135200 kg ÷ 5000
#
# MEDIUM(22-45yr):Heating = 215697 + 227890 MJ÷3.6 = 279000 kWh ÷ 5000
#                 Cooling = 86750 kWh ÷ 5000
#                 CO2     = 1197570 kg ÷ 5000
#
# NEW   (<22yr):  Heating = 132114 + 202810 MJ÷3.6 = 188450 kWh ÷ 5000
#                 Cooling = 69603 kWh ÷ 5000
#                 CO2     = 934096 kg ÷ 5000

VINTAGES = {
    "new": {
        "label":          "New construction (<22 years)",
        "heating_kwh_m2": 189123.61 / REFERENCE_AREA,  # (95.462+40.707) MJ/m² ÷3.6 ×5000
        "cooling_kwh_m2":  69851.39 / REFERENCE_AREA,  # 50.293 MJ/m² ÷3.6 ×5000
        "co2_kg_m2":      934095.65 / REFERENCE_AREA,  # row 231 New.xlsx
    },
    "medium": {
        "label":          "Existing post-1980 (22–45 years)",
        "heating_kwh_m2": 279997.22 / REFERENCE_AREA,  # (155.857+45.741) MJ/m² ÷3.6 ×5000
        "cooling_kwh_m2":  87059.72 / REFERENCE_AREA,  # 62.683 MJ/m² ÷3.6 ×5000
        "co2_kg_m2":     1197570.0  / REFERENCE_AREA,  # row 231 Medium.xlsx
    },
    "old": {
        "label":          "Existing pre-1980 (>45 years)",
        "heating_kwh_m2": 222613.89 / REFERENCE_AREA,  # 801410 MJ ÷3.6 (gas heating)
        "cooling_kwh_m2":  70130.56 / REFERENCE_AREA,  # row 137 Old.xlsx (elec cooling kWh)
        "co2_kg_m2":     1135200.0  / REFERENCE_AREA,  # row 303 Old.xlsx
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
    df["postal_code"]       = df["postal_code"].astype(str).str.strip().str.upper()
    df["building_type"]     = df["building_type"].astype(str).str.strip().str.lower()
    df["footprint_area_m2"] = pd.to_numeric(df["footprint_area_m2"], errors="coerce")
    df = df[~df["building_type"].isin(["no", "true", "false", "nan", "none", ""])]
    df_buildings = df
    print(f"Dataset loaded: {len(df)} rows")


class LookupRequest(BaseModel):
    postal_code:       str
    footprint_area_m2: float
    building_age:      float = 0


@app.post("/lookup")
def lookup_building(req: LookupRequest):
    if df_buildings is None:
        raise HTTPException(503, "Dataset not loaded")

    code = req.postal_code.strip().upper()[:3]

    in_postal = df_buildings[df_buildings["postal_code"].str.startswith(code)]
    if in_postal.empty:
        in_postal = df_buildings[df_buildings["postal_code"].str.startswith(code[:2])]
    if in_postal.empty:
        in_postal = df_buildings[df_buildings["postal_code"].str.startswith(code[:1])]
    if in_postal.empty:
        raise HTTPException(404, f"No data found for postal code {code}")

    buildings = in_postal[in_postal["building_type"] == TARGET_TYPE].copy()
    if buildings.empty:
        buildings = in_postal.copy()

    buildings["_diff"] = (buildings["footprint_area_m2"] - req.footprint_area_m2).abs()
    b = buildings.sort_values("_diff").iloc[0]

    # Select vintage
    age = req.building_age
    if age > 45:
        vintage_key = "old"
    elif age >= 22:
        vintage_key = "medium"
    else:
        vintage_key = "new"

    v         = VINTAGES[vintage_key]
    user_area = req.footprint_area_m2

    scaled_heating = v["heating_kwh_m2"] * user_area
    scaled_cooling = v["cooling_kwh_m2"] * user_area
    scaled_co2     = v["co2_kg_m2"]      * user_area

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
            "co2_kg_m2":          round(v["co2_kg_m2"],      4),
            "scaled_heating_kwh": round(scaled_heating, 2),
            "scaled_cooling_kwh": round(scaled_cooling, 2),
            "scaled_co2_kg":      round(scaled_co2,     2),
            "user_area_m2":       user_area,
        }
    }


@app.post("/parse-csv")
async def parse_csv(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents), encoding="latin1", sep=";")
        df.columns = [c.strip() for c in df.columns]
        months = []
        for _, row in df.iterrows():
            try:
                kwh    = float(row["kWh"])       if pd.notna(row["kWh"])       else 0
                amount = float(row["Amount ($)"]) if pd.notna(row["Amount ($)"]) else 0
                start  = str(row["Starting date"])[:7]
                months.append({"month": start, "kwh": round(kwh, 2), "amount": round(amount, 2)})
            except:
                continue
        if not months:
            raise HTTPException(400, "No valid billing data found in CSV")
        total_kwh    = sum(m["kwh"]    for m in months)
        total_amount = sum(m["amount"] for m in months)
        return {
            "months":       months,
            "total_kwh":    round(total_kwh, 2),
            "total_amount": round(total_amount, 2),
            "avg_amount":   round(total_amount / len(months), 2) if months else 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not parse CSV: {str(e)}")


@app.get("/")
def health():
    return {"status": "ok", "dataset_loaded": df_buildings is not None}
