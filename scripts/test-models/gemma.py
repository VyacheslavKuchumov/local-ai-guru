#!/usr/bin/env python3
import sys
sys.dont_write_bytecode = True
from smoke import main
raise SystemExit(main('gemma'))
