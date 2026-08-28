#!/bin/bash
cd "$(dirname "$0")/backend"
open http://localhost:8000 &
uvicorn main:app --reload
