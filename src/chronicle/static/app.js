"use strict";
const $ = id => document.getElementById(id);
let token = "", documents = null, catalogGeneration = 0, catalogReady = false;
const labels = {employment_delta:"Monthly employment change", production_mean3:"Production index, three-month mean"};
const datasetLabels = {"chronicle-synthetic-v1":"Synthetic example", "macro-archive-frb-v1":"Federal Reserve production archives", "macro-archive-bls-v1":"BLS employment archives"};
const sourceLabels = {synthetic:"Synthetic", "frb.g17.archive":"Federal Reserve G.17", "bls.ces.archive":"BLS"};
const seriesLabels = {employment:"Employment", production:"Industrial production"};
const modeLabels = {system_snapshot:"Named snapshot", source_as_of:"Source cutoff date", latest_corrected:"Latest within snapshot"};
const unitLabels = {thousand_persons:"Thousand persons", persons:"Persons", index:"Index"};
const statusLabels = {value:"Value", missing:"Source marks missing", withdrawn:"Source withdrawn"};
const warningLabels = {unit_mismatch:"Unit or metadata mismatch", unknown_source_availability:"Unknown release date"};
function displayDecimal(value) {
  if (value === null || value === undefined) return "—";
  return String(value).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}
function cellMessage(cell) {
  return cell.value !== null && cell.value !== undefined ? displayDecimal(cell.value) :
    cell.reasons.find(reason => reason.display_status === cell.status)?.message_zh ?? cell.status;
}
function warningMessage(warnings) {return (warnings || []).map(w => warningLabels[w] || w).join("; ");}
function unitMessage(unit) {return unit ? `${unitLabels[unit.name] || unit.name} / ${unit.metadata_version}` : "Value unavailable";}
function seriesLabel(item) {return `${seriesLabels[item.series_key] || item.series_key} · ${sourceLabels[item.source_namespace] || item.source_namespace}`;}
function snapshotLabel(id) {
  if (id === "S1") return "Initial version · S1";
  if (id === "S2") return "Revised version · S2";
  const match = /^(FRB|BLS)-(\d{4}-\d{2}-\d{2})$/.exec(id);
  return match ? `${match[1] === "FRB" ? "Federal Reserve" : "Employment release"} · ${match[2]}` : id;
}
async function request(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {Authorization:`Bearer ${token}`, ...(body ? {"Content-Type":"application/json"} : {})},
    ...(body ? {body:JSON.stringify(body)} : {})
  });
  let result;
  try {result = JSON.parse(await response.text());} catch {result = null;}
  const requestId = response.headers.get("X-Request-ID");
  if (!response.ok || !result || typeof result !== "object") {
    const code = typeof result?.code === "string" ? result.code : `HTTP ${response.status}`;
    throw new Error(`${code}${requestId ? ` · Request ${requestId}` : ""}`);
  }
  return result;
}
function text(tag, value) {const node=document.createElement(tag);node.textContent=value??"—";return node;}
function option(value, label) {const node=text("option", label);node.value=value;return node;}
function updateFormula() {
  const selected = $("series").selectedOptions[0];
  if (selected?.dataset.key === "employment") $("formula").value = "employment_delta";
  if (selected?.dataset.key === "production") $("formula").value = "production_mean3";
}
async function loadCatalog() {
  const generation = ++catalogGeneration;
  const dataset = $("dataset").value;
  documents = null;catalogReady = false;$("results").hidden = true;$("run").disabled = true;
  $("notice").textContent = "Loading series and snapshots…";
  for (const id of ["series", "left-snapshot", "right-snapshot"]) {$(id).disabled=true;$(id).replaceChildren();}
  try {
    const root = `/v1/datasets/${encodeURIComponent(dataset)}`;
    const [catalog, snapshots] = await Promise.all([request(root+"/series"), request(root+"/snapshots")]);
    if (generation !== catalogGeneration) return;
    $("series").replaceChildren(...catalog.series.map(item => {
      const node = option(JSON.stringify([item.source_namespace,item.series_key]), seriesLabel(item));
      node.dataset.namespace=item.source_namespace;node.dataset.key=item.series_key;return node;
    }));
    for (const id of ["left-snapshot", "right-snapshot"]) {
      $(id).replaceChildren(...snapshots.snapshots.map(snapshot => option(snapshot, snapshotLabel(snapshot))));
    }
    $("left-snapshot").selectedIndex = snapshots.snapshots.length > 1 ? 1 : 0;
    $("right-snapshot").selectedIndex = 0;
    catalogReady = catalog.series.length > 0 && snapshots.snapshots.length > 0;
    for (const id of ["series", "left-snapshot", "right-snapshot"]) $(id).disabled = !catalogReady;
    updateFormula();
    $("notice").textContent = !catalogReady ? "This dataset has no queryable series or snapshots." :
      catalog.truncated ? "Showing the first 100 series. Use the CLI for the remaining series." : "";
  } catch (error) {if (generation === catalogGeneration) $("notice").textContent=error.message;}
  finally {if (generation === catalogGeneration) $("run").disabled=!catalogReady;}
}
function evidence(target, report) {
  const pane=$(target);pane.replaceChildren();
  pane.append(text("p", `${snapshotLabel(report.snapshot_id)} · ${modeLabels[report.mode] || report.mode}`));
  for (const input of report.rows[0].inputs) {
    const details=document.createElement("details"), revision=input.selected_revision||input.blocking_revision;
    details.append(text("summary", `${input.observation_month.slice(0,7)} · ${input.value !== null ? displayDecimal(input.value) : input.display.message_zh}`));
    const list=document.createElement("dl");
    const fields={"Source status":statusLabels[input.source_status]||input.source_status, "Unit":unitMessage(input.unit), "Release date":revision?.availability?.date, "Revision":revision?.source_revision_key, "Source order":revision?.source_order, "Batch / receipt time":revision?.occurrences?.map(o=>`${o.batch_id} / ${o.received_at}`).join("\n"), "Report ID":report.report_id};
    for (const [key,value] of Object.entries(fields)) list.append(text("dt",key),text("dd",value));
    details.append(list);
    if(input.display.warnings.length) details.append(text("p",warningMessage(input.display.warnings)));
    pane.append(details);
  }
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();$("notice").textContent="";token=$("token").value;
  try {
    const info=await request("/v1/datasets");
    $("dataset").replaceChildren(...info.datasets.map(id=>option(id,datasetLabels[id]||id)));
    $("identity").textContent=info.role==="viewer"?"Viewer":"Operator";
    $("token").value="";$("login").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;
    await loadCatalog();
  } catch(error) {token="";$("notice").textContent=error.message;}
});
$("logout").addEventListener("click",()=>{token="";documents=null;location.reload();});
$("dataset").addEventListener("change",loadCatalog);
$("series").addEventListener("change",updateFormula);
$("mode").addEventListener("change",()=>{$("cutoff-label").hidden=$("mode").value!=="source_as_of";});
$("report-form").addEventListener("submit", async event => {
  event.preventDefault();if(!catalogReady)return;
  $("notice").textContent="";$("run").disabled=true;$("results").hidden=true;documents=null;
  const generation=catalogGeneration;
  try {
    const root=`/v1/datasets/${encodeURIComponent($("dataset").value)}`, formula=$("formula").value;
    const selected=$("series").selectedOptions[0];
    const parameters={mode:$("mode").value,source_namespace:selected.dataset.namespace,series_key:selected.dataset.key,months:[$("month").value+"-01"],exclusive_end_date:$("mode").value==="source_as_of"?$("cutoff").value:null};
    const build=async snapshot=>{
      const context_id=crypto.randomUUID();
      await request(root+"/contexts",{context_id,snapshot_id:snapshot,...parameters});
      return request(root+"/reports",{context_id,report_id:crypto.randomUUID(),formula});
    };
    const [left,right]=await Promise.all([build($("left-snapshot").value),build($("right-snapshot").value)]);
    const comparison=await request(root+"/exact-comparisons",{old_report_id:left.report_id,new_report_id:right.report_id});
    if(generation!==catalogGeneration)return;
    documents={left,right,root};const row=document.createElement("tr");row.append(text("td",`${labels[formula]} · ${parameters.months[0].slice(0,7)}`));
    for(const report of [left,right]) {
      const cell=report.rows[0],td=text("td",cellMessage(cell));td.append(text("small",unitMessage(cell.unit)));
      if(cell.warnings.length)td.append(text("small",warningMessage(cell.warnings)));row.append(td);
    }
    const difference=comparison.rows[0],unavailable=difference.status==="unit_mismatch"?"Incompatible units":"Not comparable";
    row.append(text("td",difference.stored_output_difference!==null?displayDecimal(difference.stored_output_difference):unavailable));
    const exact=text("td",difference.exact_difference!==null?displayDecimal(difference.exact_difference):unavailable);
    if(difference.exact_difference_rational){const r=difference.exact_difference_rational;exact.append(text("small",r.denominator==="1"?"Exact integer":`Exact value ${r.numerator}/${r.denominator}`));}
    if(difference.rounding_gap!==null&&difference.rounding_gap!=="0.000000000000")exact.append(text("small",`Difference from stored result: ${displayDecimal(difference.rounding_gap)} (stored result rounds twice)`));
    row.append(exact);
    $("rows").replaceChildren(row);$("left-label").textContent=`Left · ${snapshotLabel(left.snapshot_id)}`;$("right-label").textContent=`Right · ${snapshotLabel(right.snapshot_id)}`;
    evidence("left-evidence",left);evidence("right-evidence",right);$("results").hidden=false;
  } catch(error) {if(generation===catalogGeneration)$("notice").textContent=error.message;}
  finally {if(generation===catalogGeneration)$("run").disabled=!catalogReady;}
});
$("export").addEventListener("click",async()=>{
  if(!documents)return;
  try {
    const approved=await request(documents.root+"/comparison-export",{old_report_id:documents.left.report_id,new_report_id:documents.right.report_id});
    const url=URL.createObjectURL(new Blob([JSON.stringify(approved,null,2)],{type:"application/json"}));
    const link=document.createElement("a");link.href=url;link.download="chronicle-comparison.json";link.click();URL.revokeObjectURL(url);
  } catch(error) {$("notice").textContent=error.message;}
});
