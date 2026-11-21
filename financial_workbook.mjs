import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputPath, previewDir] = process.argv.slice(2);
const data = JSON.parse(await fs.readFile(inputPath, "utf8"));
const workbook = Workbook.create();
const summary = workbook.worksheets.add("Summary");
const scenarios = workbook.worksheets.add("Scenarios");
const schedule = workbook.worksheets.add("Cost Schedule");
const review = workbook.worksheets.add("Forecast Review");
const source = workbook.worksheets.add("ERP Inputs");
const checks = workbook.worksheets.add("Checks");

const navy = "#173347";
const ink = "#172A37";
const teal = "#17645F";
const green = "#146446";
const muted = "#576B77";
const line = "#CAD8DE";
const soft = "#EDF5F6";
const amber = "#FFF1CE";
const blue = "#174F9A";
const red = "#8E332A";
const basic = '#,##0;(#,##0);"-"';
const dollars = '"$"#,##0.00;("$"#,##0.00);"-"';
const wholeDollars = '"$"#,##0;("$"#,##0);"-"';
const unitPrice = '"$"0.0000;("$"0.0000);"-"';
const percent = '0.0%;(0.0%);"-"';
const dayFormat = "dd-mmm-yy";
const monthFormat = "mmm-yy";

const cell = (sheet, address, value) => { sheet.getRange(address).values = [[value]]; };
const formula = (sheet, address, value) => { sheet.getRange(address).formulas = [[value]]; };

function widths(sheet, columns) {
  for (const [column, width] of Object.entries(columns)) {
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  }
}

function setup(sheet, end, widthsByColumn) {
  sheet.showGridLines = false;
  const range = sheet.getRange(`A1:${end}`);
  range.format.font = { name: "Arial", size: 10, color: ink };
  range.format.rowHeight = 16;
  range.format.verticalAlignment = "center";
  widths(sheet, widthsByColumn);
}

function title(sheet, start, end, label, subtitle) {
  cell(sheet, `${start}2`, label);
  const titleRow = sheet.getRange(`${start}2:${end}2`);
  titleRow.format.font = { name: "Arial", size: 16, bold: true, color: navy };
  titleRow.format.rowHeight = 26;
  titleRow.format.borders = { bottom: { style: "thin", color: teal } };
  if (subtitle) {
    cell(sheet, `${start}3`, subtitle);
    sheet.getRange(`${start}3:${end}3`).format.font = { name: "Arial", size: 10, italic: true, color: muted };
    sheet.getRange(`${start}3:${end}3`).format.rowHeight = 20;
  }
}

function header(sheet, address, labels, left = false) {
  const range = sheet.getRange(address);
  range.values = [labels];
  range.format.fill = navy;
  range.format.font = { name: "Arial", size: 10, bold: true, color: "#FFFFFF" };
  range.format.horizontalAlignment = left ? "left" : "center";
  range.format.wrapText = true;
  range.format.rowHeight = 28;
}

function striped(sheet, start, end, left, right) {
  for (let row = start + 1; row <= end; row += 2) {
    sheet.getRange(`${left}${row}:${right}${row}`).format.fill = "#F4F8F9";
  }
}

function total(sheet, address) {
  const range = sheet.getRange(address);
  range.format.fill = soft;
  range.format.font = { name: "Arial", size: 10, bold: true, color: navy };
  range.format.borders = { top: { style: "thin", color: line } };
}

function formats(sheet, groups) {
  for (const [format, ranges] of groups) {
    for (const address of ranges) sheet.getRange(address).setNumberFormat(format);
  }
}

function inputStyle(sheet, address) {
  const range = sheet.getRange(address);
  range.format.fill = amber;
  range.format.font = { name: "Arial", size: 10, bold: true, color: blue };
  range.format.rowHeight = 20;
}

function unfavorable(sheet, address) {
  sheet.getRange(address).conditionalFormats.add("cellIs", {
    operator: "greaterThan", formula: 0, format: { fill: "#FCEBE8", font: { color: red, bold: true } },
  });
}

const lastSource = data.inputs.length + 6;
const lastSchedule = lastSource;
const lastForecast = data.forecasts.length + 6;
const firstYear = data.years[0];
const lastYear = data.years.at(-1);

setup(source, `P${lastSource}`, {
  A: 12, B: 9, C: 13, D: 12, E: 14, F: 13, G: 12, H: 15, I: 14, J: 15, K: 17,
  L: 15, M: 15, N: 15, O: 16, P: 17,
});
source.tabColor = "#DDCBAE";
title(source, "A", "P", "ERP inputs", "Monthly aggregation from production_batches.csv and cost_center_budgets.csv. Costs in USD.");
header(source, "A6:P6", [
  "Month", "Year", "Cost center", "Product", "Good output kg", "Scrap kg", "Batch count", "Material actual",
  "Labor actual", "Overhead actual", "ERP actual total", "Planned output kg", "Material budget",
  "Labor budget", "Overhead budget", "ERP budget total",
]);
source.getRange(`A7:P${lastSource}`).values = data.inputs;
striped(source, 7, lastSource, "A", "P");
formats(source, [[monthFormat, [`A7:A${lastSource}`]], [basic, [`E7:G${lastSource}`, `L7:L${lastSource}`]],
  [dollars, [`H7:K${lastSource}`, `M7:P${lastSource}`]]]);
source.freezePanes.freezeRows(6);

setup(schedule, `T${lastSchedule}`, {
  A: 3, B: 3, C: 12, D: 13, E: 12, F: 13, G: 12, H: 15, I: 14, J: 15, K: 16,
  L: 14, M: 14, N: 15, O: 14, P: 15, Q: 16, R: 14, S: 17, T: 13,
});
title(schedule, "C", "T", "Monthly manufacturing cost schedule",
  "Actual production costs and budget by cost center and product. Positive cost variance is unfavorable.");
header(schedule, "C6:T6", [
  "Month", "Cost center", "Product", "Good output kg", "Scrap kg", "Material actual", "Labor actual",
  "Overhead actual", "Total actual", "Actual $/kg", "Planned output kg", "Material budget",
  "Labor budget", "Overhead budget", "Total budget", "Budget $/kg", "Variance USD", "Variance %",
]);
const scheduleRows = data.inputs.map((_, index) => {
  const r = index + 7;
  const from = name => `='ERP Inputs'!${name}${r}`;
  return [
    from("A"), from("C"), from("D"), from("E"), from("F"), from("H"), from("I"), from("J"),
    `=SUM(H${r}:J${r})`, `=IF(F${r}=0,"n.a.",K${r}/F${r})`, from("L"), from("M"), from("N"), from("O"),
    `=SUM(N${r}:P${r})`, `=IF(M${r}=0,"n.a.",Q${r}/M${r})`, `=K${r}-Q${r}`,
    `=IF(Q${r}=0,"n.a.",S${r}/Q${r})`,
  ];
});
schedule.getRange(`C7:T${lastSchedule}`).formulas = scheduleRows;
striped(schedule, 7, lastSchedule, "C", "T");
for (const band of ["C7:J", "M7:P"]) schedule.getRange(`${band}${lastSchedule}`).format.font.color = green;
formats(schedule, [
  [monthFormat, [`C7:C${lastSchedule}`]], [basic, [`F7:G${lastSchedule}`, `M7:M${lastSchedule}`]],
  [dollars, [`H7:L${lastSchedule}`, `N7:S${lastSchedule}`]], [percent, [`T7:T${lastSchedule}`]],
]);
unfavorable(schedule, `S7:T${lastSchedule}`);
schedule.freezePanes.freezeRows(6);

setup(summary, "I28", { A: 3, B: 3, C: 29, D: 17, E: 17, F: 17, G: 15, H: 16, I: 15 });
summary.tabColor = navy;
title(summary, "C", "I", "Manufacturing financial model");
cell(summary, "C5", "Reporting year");
cell(summary, "D5", lastYear);
summary.getRange("D5").dataValidation = {
  rule: { type: "whole", operator: "between", formula1: firstYear, formula2: lastYear },
};
inputStyle(summary, "D5");
formula(summary, "E5", `=IF(COUNTIFS('ERP Inputs'!$B$7:$B$${lastSource},$D$5)=0,"Select an available year","")`);
summary.getRange("E5").format.font.color = red;
header(summary, "C7:G7", ["Annual cost build", "Actual", "Budget", "Variance USD", "Variance %"]);
const monthRange = `'Cost Schedule'!$C$7:$C$${lastSchedule}`;
const sourceSum = (column, start, stop) => {
  const values = `'Cost Schedule'!$${column}$7:$${column}$${lastSchedule}`;
  return `SUMIFS(${values},${monthRange},">="&${start},${monthRange},"<"&${stop})`;
};
const annualSum = column => `=IF($E$5<>"","",${sourceSum(column, "DATE($D$5,1,1)", "DATE($D$5+1,1,1)")})`;
summary.getRange("C8:C13").values = [
  ["Materials"], ["Labor"], ["Overhead"], ["Total production cost"], ["Good output kg"], ["Cost per kg"],
];
summary.getRange("D8:E10").formulas = [["H", "N"], ["I", "O"], ["J", "P"]].map(row => row.map(annualSum));
summary.getRange("D11:E11").formulas = [[`=IF($E$5<>"","",SUM(D8:D10))`, `=IF($E$5<>"","",SUM(E8:E10))`]];
summary.getRange("D12:E12").formulas = [[annualSum("F"), annualSum("M")]];
summary.getRange("D13:E13").formulas = [[`=IF($E$5<>"","",IF(D12=0,"n.a.",D11/D12))`, `=IF($E$5<>"","",IF(E12=0,"n.a.",E11/E12))`]];
summary.getRange("F8:G11").formulas = Array.from({ length: 4 }, (_, i) => {
  const r = i + 8;
  return [`=IF($E$5<>"","",D${r}-E${r})`, `=IF($E$5<>"","",IF(E${r}=0,"n.a.",F${r}/E${r}))`];
});
total(summary, "C11:G11");
cell(summary, "C15", "Monthly cost and volume");
summary.getRange("C15").format.font = { name: "Arial", size: 11, bold: true, color: teal };
header(summary, "C16:I16", ["Month", "Total actual", "Total budget", "Variance USD", "Variance %", "Good output kg", "Actual $/kg"]);
summary.getRange("C17:I28").formulas = Array.from({ length: 12 }, (_, index) => {
  const r = index + 17;
  const dateCell = `C${r}`;
  const whenYear = value => `=IF($E$5<>"","",${value})`;
  const monthSum = column => whenYear(sourceSum(column, dateCell, `DATE($D$5,${index + 2},1)`));
  return [whenYear(`DATE($D$5,${index + 1},1)`), monthSum("K"), monthSum("Q"),
    whenYear(`D${r}-E${r}`), whenYear(`IF(E${r}=0,"n.a.",F${r}/E${r})`),
    monthSum("F"), whenYear(`IF(H${r}=0,"n.a.",D${r}/H${r})`)];
});
striped(summary, 17, 28, "C", "I");
formats(summary, [
  [wholeDollars, ["D8:F11", "D17:F28"]], [dollars, ["D13:E13", "I17:I28"]],
  [basic, ["D12:E12", "H17:H28"]], [monthFormat, ["C17:C28"]], [percent, ["G8:G11", "G17:G28"]],
]);
unfavorable(summary, "F8:G11");
unfavorable(summary, "F17:G28");

setup(scenarios, "H25", { A: 3, B: 3, C: 28, D: 20, E: 18, F: 18, G: 17, H: 17 });
scenarios.tabColor = teal;
title(scenarios, "C", "H", "Production cost scenarios");
cell(scenarios, "C5", "Selected case");
const cases = ["Base", "Cost pressure", "Lower volume", "Custom"];
cell(scenarios, "D5", cases[1]);
scenarios.getRange("D5").dataValidation = { rule: { type: "list", values: cases } };
inputStyle(scenarios, "D5");
cell(scenarios, "F5", "Reporting year");
formula(scenarios, "G5", "='Summary'!D5");
scenarios.getRange("G5").format.font.color = green;
header(scenarios, "C8:H8", ["Assumption", "Active", ...cases]);
scenarios.getRange("C9:C12").values = [
  ["Material price change"], ["Production volume change"], ["Labor rate change"], ["Fixed share of overhead"],
];
scenarios.getRange("E9:H12").values = [
  [0, 0.08, 0, 0.03], [0, 0, -0.10, 0.05], [0, 0.05, 0, 0.02], [0.40, 0.40, 0.40, 0.40],
];
inputStyle(scenarios, "E9:H12");
scenarios.getRange("D9:D12").formulas = Array.from({ length: 4 }, (_, index) => {
  const r = index + 9;
  const picked = `INDEX($E${r}:$H${r},1,MATCH($D$5,$E$8:$H$8,0))`;
  return [`=IF(COUNTIFS($E$8:$H$8,$D$5)=0,"",IF(COUNT(${picked})=1,${picked},""))`];
});
formula(scenarios, "C14", `=IF('Summary'!E5<>"","Select a reporting year",IF(AND(COUNT(D9:D12)=4,MIN(D9:D11)>=-1,D12>=0,D12<=1),"","Check case inputs and percentages"))`);
scenarios.getRange("C14").format.font.color = red;
header(scenarios, "C18:G18", ["Cost build", "Historical basis", "Scenario", "Change", "Change %"]);
scenarios.getRange("C19:C25").values = [
  ["Good output kg"], ["Materials"], ["Labor"], ["Variable overhead"], ["Fixed overhead"],
  ["Total production cost"], ["Cost per kg"],
];
const scenarioReady = value => `=IF($C$14<>"","",${value})`;
const historical = row => `=IF('Summary'!E5<>"","",'Summary'!D${row})`;
scenarios.getRange("D19:D25").formulas = [
  [historical(12)], [historical(8)], [historical(9)],
  [scenarioReady("'Summary'!D10*(1-$D$12)")], [scenarioReady("'Summary'!D10*$D$12")],
  [historical(11)], [historical(13)],
];
scenarios.getRange("E19:E25").formulas = [
  ["D19*(1+$D$10)"], ["D20*(1+$D$10)*(1+$D$9)"], ["D21*(1+$D$10)*(1+$D$11)"],
  ["D22*(1+$D$10)"], ["D23"], ["SUM(E20:E23)"], ['IF(E19=0,"n.a.",E24/E19)'],
].map(([expression]) => [scenarioReady(expression)]);
scenarios.getRange("F19:G25").formulas = Array.from({ length: 7 }, (_, i) => {
  const r = 19 + i;
  return [scenarioReady(`IF(ISNUMBER(E${r}),E${r}-D${r},"n.a.")`),
    scenarioReady(`IF(AND(ISNUMBER(E${r}),D${r}<>0),F${r}/D${r},"n.a.")`)];
});
for (const address of ["D19:D23", "D25"]) scenarios.getRange(address).format.font.color = green;
total(scenarios, "C24:G24");
formats(scenarios, [[percent, ["D9:H12", "G19:G25"]], [basic, ["D19:F19"]],
  [wholeDollars, ["D20:F24"]], [dollars, ["D25:F25"]]]);
unfavorable(scenarios, "F20:G25");

setup(review, `O${lastForecast}`, { A: 3, B: 3, C: 26, D: 21, E: 12, F: 14, G: 14, H: 14,
  I: 11, J: 17, K: 17, L: 17, M: 14, N: 13, O: 15 });
title(review, "C", "O", "Forecast comparison",
  "Forecasts and model_metrics CSV exports. Prior actuals span the same number of days through the issue date.");
cell(review, "C5", "Forecasts issued");
cell(review, "D5", data.cutoff);
review.getRange("D5").setNumberFormat(dayFormat);
header(review, "C6:O6", ["Metric", "ERP scope", "Days", "Issued", "Window start", "Window end", "Unit",
  "XGBoost forecast", "Prior actual", "Change vs prior", "Change %", "WAPE", "Backtest windows"]);
review.getRange(`C7:K${lastForecast}`).values = data.forecasts.map(row => row.slice(0, 9));
review.getRange(`N7:O${lastForecast}`).values = data.forecasts.map(row => row.slice(9));
review.getRange(`L7:M${lastForecast}`).formulas = data.forecasts.map((_, index) => {
  const r = index + 7;
  return [`=IF(ISNUMBER(K${r}),J${r}-K${r},"n.a.")`,
    `=IF(AND(ISNUMBER(K${r}),K${r}<>0),L${r}/ABS(K${r}),"n.a.")`];
});
striped(review, 7, lastForecast, "C", "O");
formats(review, [[dayFormat, [`F7:H${lastForecast}`]], [dollars, [`J7:L${lastForecast}`]],
  [percent, [`M7:N${lastForecast}`]], [basic, [`O7:O${lastForecast}`]]]);
for (const [index, row] of data.forecasts.entries()) {
  if (row[6] === "USD/kg") review.getRange(`J${index + 7}:L${index + 7}`).setNumberFormat(unitPrice);
}
review.freezePanes.freezeRows(6);

setup(checks, "F10", { A: 3, B: 3, C: 34, D: 18, E: 18, F: 16 });
title(checks, "C", "F", "Schedule reconciliation", "USD unless indicated. Covers all imported months.");
header(checks, "C6:F6", ["Measure", "Schedule total", "ERP input total", "Difference"]);
checks.getRange("C7:C10").values = [["Production cost"], ["Budgeted cost"], ["Good output kg"], ["Planned output kg"]];
checks.getRange("D7:F10").formulas = [["K", "K"], ["Q", "P"], ["F", "E"], ["M", "L"]].map(([build, original], i) => [
  `=SUM('Cost Schedule'!${build}7:${build}${lastSchedule})`,
  `=SUM('ERP Inputs'!${original}7:${original}${lastSource})`, `=ROUND(D${i + 7}-E${i + 7},2)`,
]);
checks.getRange("F7:F10").conditionalFormats.addCustom("ABS(F7)>0.005", {
  fill: "#FCEBE8", font: { color: red, bold: true },
});
formats(checks, [[dollars, ["D7:E8"]], [basic, ["D9:E10"]], ["#,##0.00;(#,##0.00);0.00", ["F7:F10"]]]);

workbook.recalculate();
const checkValues = checks.getRange("F7:F10").values.flat();
if (!checkValues.every(value => value === 0)) throw Error(`Schedule reconciliation failed: ${checkValues}`);
const annual = summary.getRange("D11:E11").values[0];
const expectedAnnual = data.inputs.filter(row => row[1] === lastYear)
  .reduce(([actual, budget], row) => [actual + row[10], budget + row[15]], [0, 0]);
if (!annual.every((value, index) => Math.abs(value - expectedAnnual[index]) < 0.005)) {
  throw Error(`Annual totals do not reconcile: ${annual} vs ${expectedAnnual}`);
}
const expectedScenario = summary.getRange("D8:D10").values.flat();
const expectedCost = expectedScenario[0] * 1.08 + expectedScenario[1] * 1.05 + expectedScenario[2];
if (Math.abs(scenarios.getRange("E24").values[0][0] - expectedCost) >= 0.005) {
  throw Error("The selected cost scenario does not reconcile.");
}
const errors = await workbook.inspect({
  kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 20 }, summary: "Final formula errors",
});
console.log(errors.ndjson);
if (previewDir) {
  await fs.mkdir(previewDir, { recursive: true });
  for (const [sheet, range] of [
    [summary, "A1:I28"], [scenarios, "A1:H25"], [schedule, "A1:T13"],
    [review, "A1:O16"], [source, "A1:P13"], [checks, "A1:F10"],
  ]) {
    const png = await workbook.render({ sheetName: sheet.name, range, format: "png", scale: 1 });
    await fs.writeFile(path.join(previewDir, `${sheet.name}.png`), new Uint8Array(await png.arrayBuffer()));
  }
}
await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
