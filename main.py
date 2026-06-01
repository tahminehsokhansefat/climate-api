"""
Montreal Climate Zone Lookup — FastAPI Backend
----------------------------------------------
Install:  pip install fastapi uvicorn pandas openpyxl python-multipart
Run:      uvicorn main:app --reload
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import io
import os

app = FastAPI(title="Montreal Climate Lookup API")

# ── CORS ─────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Load dataset ─────────────────────────────────────────────────────
EXCEL_PATH = "dataset.xlsx"
TARGET_TYPE = "office"

df_buildings: pd.DataFrame | None = None


@app.on_event("startup")
def load_dataset():
    global df_buildings

    if not os.path.exists(EXCEL_PATH):
        print(f"WARNING: {EXCEL_PATH} not found.")
        return

    df = pd.read_excel(EXCEL_PATH)
    df.columns = [c.strip() for c in df.columns]

    df["postal_code"] = df["postal_code"].astype(str).str.strip().str.upper()
    df["building_type"] = df["building_type"].astype(str).str.strip().str.lower()

    df["footprint_area_m2"] = pd.to_numeric(df["footprint_area_m2"], errors="coerce")
    df["Heating"] = pd.to_numeric(df["Heating"], errors="coerce")
    df["Cooling"] = pd.to_numeric(df["Cooling"], errors="coerce")

    df_buildings = df
    print(f"Dataset loaded: {len(df)} rows")


# ═══════════════════════════════════════════════════════════════
# ENDPOINT 1 — Building lookup (FIXED)
# ═══════════════════════════════════════════════════════════════

class LookupRequest(BaseModel):
    postal_code: str
    footprint_area_m2: float


@app.post("/lookup")
def lookup_building(req: LookupRequest):
    if df_buildings is None:
        raise HTTPException(503, "Dataset not loaded")

    # ✅ FIX 1: PREFIX MATCH (H1H works now)
    code = req.postal_code.strip().upper()

    in_postal = df_buildings[
        df_buildings["postal_code"].str.upper().str.startswith(code)
    ]

    if in_postal.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No records found for postal code prefix '{code}'."
        )

    # ✅ FIX 2: SAFE FILTER FOR OFFICE (with fallback)
    offices = in_postal[in_postal["building_type"] == TARGET_TYPE].copy()

    if offices.empty:
        # fallback → use all building types instead of failing
        offices = in_postal.copy()

    # Find nearest by footprint area
    offices["_diff"] = (offices["footprint_area_m2"] - req.footprint_area_m2).abs()
    building = offices.sort_values("_diff").iloc[0]

    def safe(val):
        try:
            if pd.isna(val):
                return None
        except Exception:
            pass
        if isinstance(val, float) and val.is_integer():
            return int(val)
        return val

    result = {}
    for col in [
        "postal_code", "building_type", "footprint_area_m2",
        "Climate Zone", "Heating", "Cooling", "region", "lat", "lon"
    ]:
        if col in building.index:
            result[col] = safe(building[col])

    return {"building": result}


# ═══════════════════════════════════════════════════════════════
# ENDPOINT 2 — CSV parser (unchanged but safe)
# ═══════════════════════════════════════════════════════════════

MONTHS = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December"
]


@app.post("/parse-csv")
async def parse_hydro_csv(file: UploadFile = File(...)):
    contents = await file.read()

    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(contents), sep=";", encoding=enc)
            break
        except Exception:
            continue
    else:
        raise HTTPException(400, "Could not decode CSV")

    df.columns = [c.strip() for c in df.columns]

    required = ["Starting date", "kWh", "Amount ($)"]
    missing = [c for c in required if c not in df.columns]

    if missing:
        raise HTTPException(
            400,
            f"Missing columns: {missing}"
        )

    account = ""
    if "Contract" in df.columns and not df["Contract"].dropna().empty:
        account = str(df["Contract"].dropna().iloc[0]).strip()

    monthly = {}

    for _, row in df.iterrows():
        start_raw = str(row.get("Starting date", "")).strip()
        if not start_raw or start_raw.lower() == "nan":
            continue

        try:
            date = pd.to_datetime(start_raw)
            m = date.month - 1
            y = date.year
        except:
            continue

        try:
            kwh = float(str(row["kWh"]).replace(",", "."))
        except:
            kwh = 0.0

        try:
            amount = float(str(row["Amount ($)"]).replace(",", "."))
        except:
            amount = 0.0

        key = (y, m)
        if key not in monthly:
            monthly[key] = {"kwh": 0.0, "amount": 0.0}

        monthly[key]["kwh"] += kwh
        monthly[key]["amount"] += amount

    flat = {}
    for (y, m), v in sorted(monthly.items()):
        flat[m] = {
            "month": MONTHS[m],
            "kwh": v["kwh"],
            "amount": v["amount"]
        }

    if not flat:
        raise HTTPException(422, "No data extracted")

    months_list = [flat[i] for i in sorted(flat)]
    total_kwh = sum(x["kwh"] for x in months_list)
    total_amount = sum(x["amount"] for x in months_list)

    return {
        "account": account,
        "months": months_list,
        "total_kwh": round(total_kwh, 2),
        "total_amount": round(total_amount, 2),
        "avg_amount": round(total_amount / len(months_list), 2)
    }


# ═══════════════════════════════════════════════════════════════
# Health check
# ═══════════════════════════════════════════════════════════════

@app.get("/")
def health():
    return {"status": "ok", "dataset_loaded": df_buildings is not None}
