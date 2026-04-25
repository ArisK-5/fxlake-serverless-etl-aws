#!/bin/bash
set -e

# -----------------------------------------------------------
# Validate that all required source files exist before building
# -----------------------------------------------------------
REQUIRED_FILES=(
  lambda_fx_ingestion.py
  lambda_ecb_ingestion.py
  lambda_fred_ingestion.py
  lambda_validation_function.py
  lambda_iceberg_writer.py
  lambda_data_validator.py
)

echo "🔍 Validating Lambda source files..."

for f in "${REQUIRED_FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "❌ ERROR: Required source file not found: $f"
    exit 1
  fi
done

if [ ! -d "common" ]; then
  echo "❌ ERROR: Required directory not found: common/"
  exit 1
fi

echo "✅ All source files present."

# -----------------------------------------------------------
# Helper: build one Lambda zip
#   Usage: build_lambda <source_file> <zip_name> [requirements_file]
# -----------------------------------------------------------
build_lambda() {
  local source_file="$1"
  local zip_name="$2"
  local req_file="${3:-requirements.txt}"

  echo "📦 Building Lambda: ${source_file}..."

  rm -rf package "${zip_name}"
  mkdir -p package

  if [ -f "${req_file}" ]; then
    echo "  📚 Installing dependencies from ${req_file}..."
    pip3 install --target ./package -r "${req_file}"
  else
    echo "  ⚠️  No ${req_file} found — skipping dependency install"
  fi

  cp "${source_file}" package/
  cp -r common package/

  cd package
  zip -r "../${zip_name}" .
  cd ..

  rm -rf package

  # Verify zip was created and is non-empty
  if [ ! -s "${zip_name}" ]; then
    echo "❌ ERROR: ${zip_name} is missing or empty after build"
    exit 1
  fi

  echo "  ✅ Created ${zip_name} ($(du -h "${zip_name}" | cut -f1))"
}

# -----------------------------------------------------------
# Build each Lambda
# -----------------------------------------------------------
build_lambda lambda_fx_ingestion.py       lambda_fx_ingestion.zip
build_lambda lambda_ecb_ingestion.py      lambda_ecb_ingestion.zip
build_lambda lambda_fred_ingestion.py     lambda_fred_ingestion.zip
build_lambda lambda_validation_function.py lambda_validation_function.zip requirements_validation.txt
build_lambda lambda_iceberg_writer.py     lambda_iceberg_writer.zip requirements_iceberg_writer.txt

build_lambda lambda_data_validator.py     lambda_data_validator.zip

echo "  📚 Adding quality.py to iceberg writer package..."
mkdir -p _iceberg_patch
cd _iceberg_patch
unzip -q "../lambda_iceberg_writer.zip"
cp "../../glue/quality.py" .
zip -q -r "../lambda_iceberg_writer.zip" quality.py
cd ..
rm -rf _iceberg_patch

echo ""
echo "✅ Lambda packaging complete."
echo "Created: lambda_fx_ingestion.zip, lambda_ecb_ingestion.zip, lambda_fred_ingestion.zip, lambda_validation_function.zip, lambda_iceberg_writer.zip (with quality.py), lambda_data_validator.zip"
