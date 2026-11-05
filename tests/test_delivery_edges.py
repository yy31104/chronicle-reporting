"""Execute the shipped JavaScript display policy; not a visual browser audit."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_script_uses_selected_primary_reason_and_keeps_warnings():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the shipped frontend display helpers")
    script = Path(__file__).parents[1] / "src/chronicle/static/app.js"
    runner = r"""
const fs=require("fs"),vm=require("vm");
const context={document:{getElementById:()=>({addEventListener(){}})}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const row={value:null,status:"withdrawn",reasons:[
  {display_status:"not_available_at_cutoff",message_zh:"No data in this snapshot"},
  {display_status:"withdrawn",message_zh:"Source withdrew this value"}
]};
process.stdout.write(JSON.stringify({primary:context.cellMessage(row),
 warning:context.warningMessage(["unit_mismatch","unknown_source_availability"])}));
"""
    result = subprocess.run(
        [node, "-e", runner, str(script)], capture_output=True, text=True, timeout=5, check=True
    )
    assert json.loads(result.stdout) == {
        "primary": "Source withdrew this value",
        "warning": "Unit or metadata mismatch; Unknown release date",
    }


def test_frontend_display_trims_only_zeros_without_numeric_rounding_and_handles_non_json():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the shipped frontend helpers")
    script = Path(__file__).parents[1] / "src/chronicle/static/app.js"
    runner = r"""
const fs=require("fs"),vm=require("vm");
const context={document:{getElementById:()=>({addEventListener(){}})},
fetch:async()=>({ok:false,status:400,text:async()=>"Invalid host header",
headers:{get:()=>"example-request-id"}})};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
(async()=>{
let error;
try{await context.request("/bad-host");}catch(failure){error=failure.message;}
process.stdout.write(JSON.stringify({values:["10.000000000000","0.000000000000",
"102.666666666667","99999999999999999999999999.999999999999","-0.010000000000"]
.map(context.displayDecimal),error,unit:context.unitMessage({name:"thousand_persons",metadata_version:"v1"})}));
})();
"""
    result = subprocess.run(
        [node, "-e", runner, str(script)], capture_output=True, text=True, timeout=5, check=True
    )
    assert json.loads(result.stdout) == {
        "values": [
            "10",
            "0",
            "102.666666666667",
            "99999999999999999999999999.999999999999",
            "-0.01",
        ],
        "error": "HTTP 400 · Request example-request-id",
        "unit": "Thousand persons / v1",
    }
