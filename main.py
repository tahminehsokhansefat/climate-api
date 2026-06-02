"""
Montreal Climate Zone Lookup — DOE Reference Area Scaling Model
--------------------------------------------------------------
✔ Uses fixed DOE medium office baseline = 5000 m²
✔ Normalizes Heating/Cooling by reference area
✔ Scales to user input area
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

EXCEL_PATH = "dataset.xlsx"
TARGET_TYPE = "office"

REFERENCE_AREA = 5000  # ✅ YOUR DOE BASELINE

df_buildings = None


@app.on_event("startup")
def load_dataset():
    global df_buildings

    if not os.path.exists(EXCEL_PATH):
        print("Dataset not found")
        return

    df = pd.read_excel(EXCEL_PATH)
    df.columns = [c.strip() for c in df.columns]

    df["postal_code"] = df["postal_code"].astype(str).str.strip().str.upper()
    df["building_type"] = df["building_type"].astype(str).str.strip().str.lower()

    df["footprint_area_m2"] = pd.to_numeric(df["footprint_area_m2"], errors="coerce")
    df["Heating"] = pd.to_numeric(df["Heating"], errors="coerce")
    df["Cooling"] = pd.to_numeric(df["Cooling"], errors="coerce")

    df = df[~df["building_type"].isin(["yes", "no", "true", "false", "nan", "none"])]

    df_buildings = df
    print(f"Dataset loaded: {len(df)} rows")


# ─────────────────────────────────────────────
class LookupRequest(BaseModel):
    postal_code: str
    footprint_area_m2: float


# ─────────────────────────────────────────────
@app.post("/lookup")
def lookup_building(req: LookupRequest):

    if df_buildings is None:
        raise HTTPException(503, "Dataset not loaded")

    code = req.postal_code.strip().upper()

    # prefix match
    in_postal = df_buildings[
        df_buildings["postal_code"].str.startswith(code)
    ]

    if in_postal.empty:
        raise HTTPException(404, f"No data for prefix {code}")

    buildings = in_postal[in_postal["building_type"] == TARGET_TYPE]
    if buildings.empty:
        buildings = in_postal.copy()

    buildings["_diff"] = (
        buildings["footprint_area_m2"] - req.footprint_area_m2
    ).abs()

    b = buildings.sort_values("_diff").iloc[0]

    # ─────────────────────────────────────────────
    # STEP 1: normalize using REFERENCE AREA (5000 m²)
    # ─────────────────────────────────────────────

    heating_ref = b["Heating"] / REFERENCE_AREA
    cooling_ref = b["Cooling"] / REFERENCE_AREA

    # ─────────────────────────────────────────────
    # STEP 2: scale to user input area
    # ─────────────────────────────────────────────

    user_area = req.footprint_area_m2

    scaled_heating = heating_ref * user_area
    scaled_cooling = cooling_ref * user_area

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
    "footprint_area_m2": safe(b["footprint_area_m2"]),
    "climate_zone": safe(b["Climate Zone"]),
},
        "energy_model": {
            "reference_area_m2": REFERENCE_AREA,

            "heating_kwh_m2_ref": round(heating_ref, 4),
            "cooling_kwh_m2_ref": round(cooling_ref, 4),

            "scaled_heating_kwh": round(scaled_heating, 2),
            "scaled_cooling_kwh": round(scaled_cooling, 2),

            "user_area_m2": user_area
        }
    }


# ─────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "dataset_loaded": df_buildings is not None}
