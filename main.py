"""
Montreal Climate Zone Lookup — Enhanced DOE-style Energy API
------------------------------------------------------------
Features:
✔ Postal prefix matching (H1H works)
✔ Clean building type handling
✔ Energy Use Intensity (EUI)
✔ Energy scaling to new area
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import io
import os

app = FastAPI(title="Montreal Climate Energy API")

# ── CORS ───────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Load dataset ───────────────────────────────────────
EXCEL_PATH = "dataset.xlsx"
TARGET_TYPE = "office"

df_buildings = None


@app.on_event("startup")
def load_dataset():
    global df_buildings

    if not os.path.exists(EXCEL_PATH):
        print("Dataset not found")
        return

    df = pd.read_excel(EXCEL_PATH)
    df.columns = [c.strip() for c in df.columns]

    # ── Clean data ───────────────────────────────
    df["postal_code"] = df["postal_code"].astype(str).str.strip().str.upper()
    df["building_type"] = df["building_type"].astype(str).str.strip().str.lower()

    df["footprint_area_m2"] = pd.to_numeric(df["footprint_area_m2"], errors="coerce")
    df["Heating"] = pd.to_numeric(df["Heating"], errors="coerce")
    df["Cooling"] = pd.to_numeric(df["Cooling"], errors="coerce")

    # remove invalid types (fix "yes" issue)
    df = df[~df["building_type"].isin(["yes", "no", "true", "false", "nan", "none"])]

    df_buildings = df
    print(f"Dataset loaded: {len(df)} rows")


# ── Request model ─────────────────────────────────────
class LookupRequest(BaseModel):
    postal_code: str
    footprint_area_m2: float


# ───────────────────────────────────────────────────────
# BUILDING LOOKUP + EUI + SCALING
# ───────────────────────────────────────────────────────
@app.post("/lookup")
def lookup_building(req: LookupRequest):
    if df_buildings is None:
        raise HTTPException(503, "Dataset not loaded")

    # ✔ prefix match (H1H works)
    code = req.postal_code.strip().upper()

    in_postal = df_buildings[
        df_buildings["postal_code"].str.startswith(code)
    ]

    if in_postal.empty:
        raise HTTPException(404, f"No data for prefix {code}")

    # ✔ prefer office but fallback
    buildings = in_postal[in_postal["building_type"] == TARGET_TYPE]
    if buildings.empty:
        buildings = in_postal.copy()

    # ✔ nearest footprint match
    buildings["_diff"] = (buildings["footprint_area_m2"] - req.footprint_area_m2).abs()
    b = buildings.sort_values("_diff").iloc[0]

    area = b["footprint_area_m2"]

    # ── ENERGY INTENSITY (EUI) ─────────────────────
    heating_eui = b["Heating"] / area if area else None
    cooling_eui = b["Cooling"] / area if area else None

    # ── SCALE TO NEW AREA ─────────────────────────
    new_area = req.footprint_area_m2

    scaled_heating = heating_eui * new_area if heating_eui else None
    scaled_cooling = cooling_eui * new_area if cooling_eui else None

    def safe(v):
        try:
            if pd.isna(v):
                return None
        except:
            pass
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v

    return {
        "building": {
            "postal_code": b["postal_code"],
            "building_type": b["building_type"],
            "footprint_area_m2": safe(area),
            "Climate Zone": safe(b.get("Climate Zone")),
            "Heating": safe(b["Heating"]),
            "Cooling": safe(b["Cooling"]),
        },
        "energy_model": {
            "heating_eui_kwh_m2": round(heating_eui, 3) if heating_eui else None,
            "cooling_eui_kwh_m2": round(cooling_eui, 3) if cooling_eui else None,
            "scaled_heating_kwh": round(scaled_heating, 2) if scaled_heating else None,
            "scaled_cooling_kwh": round(scaled_cooling, 2) if scaled_cooling else None,
            "new_area_m2": new_area
        }
    }


# ───────────────────────────────────────────────────────
# CSV PARSER (unchanged)
# ───────────────────────────────────────────────────────
@app.post("/parse-csv")
async def parse_hydro_csv(file: UploadFile = File(...)):
    contents = await file.read()

    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(contents), sep=";", encoding=enc)
            break
        except:
            continue
    else:
        raise HTTPException(400, "CSV decode error")

    df.columns = [c.strip() for c in df.columns]

    required = ["Starting date", "kWh", "Amount ($)"]
    if any(c not in df.columns for c in required):
        raise HTTPException(400, "Missing required columns")

    account = ""
    if "Contract" in df.columns:
        account = str(df["Contract"].dropna().iloc[0])

    monthly = {}

    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(r["Starting date"])
            m = d.month - 1
            y = d.year
        except:
            continue

        kwh = float(str(r["kWh"]).replace(",", "."))
        amt = float(str(r["Amount ($)"]).replace(",", "."))

        key = (y, m)
        monthly[key] = monthly.get(key, {"kwh": 0, "amount": 0})

        monthly[key]["kwh"] += kwh
        monthly[key]["amount"] += amt

    flat = {
        m: {
            "month": ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m],
            "kwh": v["kwh"],
            "amount": v["amount"]
        }
        for (_, m), v in sorted(monthly.items())
    }

    months = list(flat.values())

    return {
        "account": account,
        "months": months,
        "total_kwh": sum(x["kwh"] for x in months),
        "total_amount": sum(x["amount"] for x in months)
    }


# ───────────────────────────────────────────────────────
# HEALTH
# ───────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "dataset_loaded": df_buildings is not None}
