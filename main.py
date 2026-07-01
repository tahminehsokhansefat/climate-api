"""
Montreal Climate Zone Lookup — DOE Reference Area Scaling Model
--------------------------------------------------------------
✔ Reads heating/cooling/CO2 directly from Excel vintage files
✔ New.xlsx    → buildings < 22 years old
✔ Medium.xlsx → buildings 22–45 years old  
✔ Old.xlsx    → buildings > 45 years old
✔ Scales to user input area
✔ Parses Hydro-Québec CSV bills
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import openpyxl
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
REFERENCE_AREA = 5000   # DOE medium office baseline (m²)
MJ_TO_KWH      = 1 / 3.6

# ── Will be populated at startup from Excel files ─────────────────────────────
VINTAGES     = {}
df_buildings = None


def read_vintage(path: str, label: str, co2_row: int) -> dict:
    """
    Read heating/cooling/CO2 from a vintage Excel file.
    New/Medium: rows 64+80 = heating, row 65 = cooling, row co2_row = CO2
    Old:        row 137 = cooling kWh, row 152 = heating MJ, row 303 = CO2
    """
    wb   = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    def val(r):
        row = rows[r - 1]
        # Find the first numeric value in the row (skip None and strings)
        for cell in row:
            if cell is not None and isinstance(cell, (int, float)) and not isinstance(cell, bool):
                return float(cell)
        return 0.0

    # Detect file type: Old.xlsx has different structure
    # Old.xlsx row 136 = Heating (=0), New/Medium row 136 = Fans (non-zero MJ/m2)
    row136_label = str(rows[135][1] or "").strip()

    if row136_label == "Heating":
        # ── Old.xlsx format ──
        # row 136 = Heating elec kWh (= 0)
        # row 137 = Cooling elec kWh
        # row 152 = Heating gas MJ
        heat_elec = val(136)
        cool_elec = val(137)
        heat_gas  = val(152) * MJ_TO_KWH
        heating   = heat_elec + heat_gas
        cooling   = cool_elec
    else:
        # ── New/Medium.xlsx format ──
        # row 64 = Heating elec kWh
        # row 65 = Cooling elec kWh
        # row 80 = Heating gas MJ
        heat_elec = val(64)
        cool_elec = val(65)
        heat_gas  = val(80) * MJ_TO_KWH
        heating   = heat_elec + heat_gas
        cooling   = cool_elec

    co2 = val(co2_row)

    return {
        "label":          label,
        "heating_kwh_m2": round(heating / REFERENCE_AREA, 6),
        "cooling_kwh_m2": round(cooling / REFERENCE_AREA, 6),
        "co2_kg_m2":      round(co2     / REFERENCE_AREA, 6),
        "heating_total":  round(heating, 2),
        "cooling_total":  round(cooling, 2),
        "co2_total":      round(co2,     2),
    }


@app.on_event("startup")
def load_dataset():
    global df_buildings, VINTAGES

    # ── Load building dataset ──
    if not os.path.exists(EXCEL_PATH):
        print("dataset.xlsx not found")
    else:
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

    # ── Load vintage energy files ──
    vintage_files = {
        "new":    ("New.xlsx",    "New construction (<22 years)",          231),
        "medium": ("Medium.xlsx", "Existing post-1980 (22–45 years)",      231),
        "old":    ("Old.xlsx",    "Existing pre-1980 (>45 years)",         303),
    }

    for key, (fname, label, co2_row) in vintage_files.items():
        if os.path.exists(fname):
            try:
                VINTAGES[key] = read_vintage(fname, label, co2_row)
                v = VINTAGES[key]
                print(f"{key}: heat={v['heating_total']:.0f} kWh  cool={v['cooling_total']:.0f} kWh  co2={v['co2_total']:.0f} kg")
            except Exception as e:
                print(f"Error reading {fname}: {e}")
        else:
            print(f"{fname} not found — using fallback values")
            # Fallback hardcoded values
            fallback = {
                "new":    {"heating_kwh_m2": 37.69,  "cooling_kwh_m2": 13.92, "co2_kg_m2": 186.82},
                "medium": {"heating_kwh_m2": 55.80,  "cooling_kwh_m2": 17.35, "co2_kg_m2": 239.51},
                "old":    {"heating_kwh_m2": 44.52,  "cooling_kwh_m2": 14.03, "co2_kg_m2": 227.04},
            }
            VINTAGES[key] = {
                "label":          label,
                **fallback[key],
                "heating_total":  fallback[key]["heating_kwh_m2"] * REFERENCE_AREA,
                "cooling_total":  fallback[key]["cooling_kwh_m2"] * REFERENCE_AREA,
                "co2_total":      fallback[key]["co2_kg_m2"]      * REFERENCE_AREA,
            }


# ─────────────────────────────────────────────
class LookupRequest(BaseModel):
    postal_code:       str
    footprint_area_m2: float
    building_age:      float = 0


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
    return {
        "status":         "ok",
        "dataset_loaded": df_buildings is not None,
        "vintages_loaded": list(VINTAGES.keys()),
    }
