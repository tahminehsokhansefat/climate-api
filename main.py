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

# ── Allow your Lovable frontend to call this API ───────────────────────────────
# Replace the URL below with your actual Lovable site URL before deploying
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your Lovable URL in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Load Excel dataset once at startup ────────────────────────────────────────
EXCEL_PATH = "dataset.xlsx"      # place this file next to main.py
TARGET_TYPE = "office"

df_buildings: pd.DataFrame | None = None

@app.on_event("startup")
def load_dataset():
    global df_buildings
    if not os.path.exists(EXCEL_PATH):
        print(f"WARNING: {EXCEL_PATH} not found. /lookup will not work.")
        return
    df = pd.read_excel(EXCEL_PATH)
    df.columns = [c.strip() for c in df.columns]
    df["postal_code"]       = df["postal_code"].astype(str).str.strip().str.upper()
    df["building_type"]     = df["building_type"].astype(str).str.strip().str.lower()
    df["footprint_area_m2"] = pd.to_numeric(df["footprint_area_m2"], errors="coerce")
    df["Heating"]           = pd.to_numeric(df["Heating"], errors="coerce")
    df["Cooling"]           = pd.to_numeric(df["Cooling"], errors="coerce")
    df_buildings = df
    print(f"Dataset loaded: {len(df)} rows")


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT 1 — Building lookup
# ══════════════════════════════════════════════════════════════════════════════

class LookupRequest(BaseModel):
    postal_code: str
    footprint_area_m2: float

@app.post("/lookup")
def lookup_building(req: LookupRequest):
    """
    Find the nearest office building to the given postal code and footprint area.
    Returns building details including climate zone, heating, cooling, etc.
    """
    if df_buildings is None:
        raise HTTPException(status_code=503, detail="Dataset not loaded on server.")

    code = req.postal_code.strip().upper()
    in_postal = df_buildings[df_buildings["postal_code"] == code]

    if in_postal.empty:
        raise HTTPException(status_code=404, detail=f"No records found for postal code '{code}'.")

    offices = in_postal[in_postal["building_type"] == TARGET_TYPE].copy()
    if offices.empty:
        types_found = in_postal["building_type"].unique().tolist()
        raise HTTPException(
            status_code=404,
            detail=f"No office buildings in '{code}'. Types available: {types_found}"
        )

    offices["_diff"] = (offices["footprint_area_m2"] - req.footprint_area_m2).abs()
    building = offices.sort_values("_diff").iloc[0]

    # Build response — only include columns that exist and are not NaN
    def safe(val):
        try:
            if pd.isna(val):
                return None
        except Exception:
            pass
        if isinstance(val, float) and val == int(val):
            return int(val)
        return val

    result = {}
    for col in ["postal_code", "building_type", "footprint_area_m2",
                "Climate Zone", "Heating", "Cooling", "region", "lat", "lon"]:
        if col in building.index:
            result[col] = safe(building[col])

    return {"building": result}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT 2 — Hydro-Québec CSV parser
# ══════════════════════════════════════════════════════════════════════════════

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

@app.post("/parse-csv")
async def parse_hydro_csv(file: UploadFile = File(...)):
    """
    Accept a Hydro-Québec semicolon-delimited CSV and return monthly billing data.

    Expected columns: Contract, Starting date, kWh, Amount ($)
    """
    contents = await file.read()

    # Try UTF-8 first, fall back to latin-1
    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(contents), sep=";", encoding=enc)
            break
        except Exception:
            continue
    else:
        raise HTTPException(status_code=400, detail="Could not decode CSV file.")

    df.columns = [c.strip() for c in df.columns]

    required = ["Starting date", "kWh", "Amount ($)"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns: {missing}. Found: {df.columns.tolist()}"
        )

    account = ""
    if "Contract" in df.columns and not df["Contract"].dropna().empty:
        account = str(df["Contract"].dropna().iloc[0]).strip()

    monthly: dict[tuple, dict] = {}

    for _, row in df.iterrows():
        start_raw = str(row.get("Starting date", "")).strip()
        if not start_raw or start_raw.lower() == "nan":
            continue
        try:
            start_date = pd.to_datetime(start_raw, dayfirst=False)
            month_idx  = start_date.month - 1
            year       = start_date.year
        except Exception:
            continue

        try:
            kwh = float(str(row["kWh"]).replace(",", ".").strip())
        except (ValueError, TypeError):
            kwh = 0.0

        try:
            amount = float(str(row["Amount ($)"]).replace(",", ".").strip())
        except (ValueError, TypeError):
            amount = 0.0

        key = (year, month_idx)
        if key not in monthly:
            monthly[key] = {"kwh": 0.0, "amount": 0.0}
        monthly[key]["kwh"]    += kwh
        monthly[key]["amount"] += amount

    # Flatten: keep latest year per month index
    flat: dict[int, dict] = {}
    for (year, midx), vals in sorted(monthly.items()):
        flat[midx] = {"month": MONTHS[midx], "kwh": vals["kwh"], "amount": vals["amount"]}

    if not flat:
        raise HTTPException(
            status_code=422,
            detail="No monthly data could be extracted. Check the CSV format."
        )

    months_list = [flat[i] for i in sorted(flat)]
    total_kwh    = sum(m["kwh"]    for m in months_list)
    total_amount = sum(m["amount"] for m in months_list)
    avg_amount   = total_amount / len(months_list)

    return {
        "account":      account,
        "months":       months_list,
        "total_kwh":    round(total_kwh, 2),
        "total_amount": round(total_amount, 2),
        "avg_amount":   round(avg_amount, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Health check
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/")
def health():
    return {"status": "ok", "dataset_loaded": df_buildings is not None}
