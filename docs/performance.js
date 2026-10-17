/* Phase 13 performance display: read-only, no live betting/odds or hidden assumptions. */
(() => {
  'use strict';
  const $ = x => document.getElementById(x);
  const n = (tag, cls, text) => {const x=document.createElement(tag); if(cls)x.className=cls; if(text!==undefined)x.textContent=String(text); return x;};
  const percent = p => `${(100*p).toFixed(1)}%`;
  const metric = p => Number(p).toFixed(5);
  const box = (title,sub) => {const x=n('section','perf-panel'); x.append(n('h2','',title)); if(sub)x.append(n('p','',sub)); return x;};
  function validate(d){
    if(d?.schema_version!==1 || d.season_train!==2024 || d.season_test!==2025 || !Array.isArray(d.models) || d.models.length!==4 || !Number.isInteger(d.sample_predictions) || d.sample_predictions<100) throw Error('Historical results file is incomplete.');
    for(const m of d.models) if(typeof m.name!=='string' || !Number.isFinite(m.brier) || !(m.brier>0 && m.brier<1) || !Number.isFinite(m.log_loss) || m.log_loss<=0) throw Error('Invalid performance metric.');
    if(!Number.isFinite(d.average_phase7_probability)||!Number.isFinite(d.actual_scoring_frequency))throw Error('Invalid scoring-rate fields.');
    return d;
  }
  function render(d){
    const root=$('perf-data'); root.replaceChildren();
    const kpis=n('div','perf-kpis');
    [[d.sample_predictions.toLocaleString('en-US'),'Historical forecasts'],[metric(d.models[3].brier),'Phase 7 Brier ↓'],[metric(d.models[3].log_loss),'Phase 7 log loss ↓']].forEach(([v,label])=>{const x=n('div','perf-kpi');x.append(n('strong','',v),n('span','',label));kpis.append(x);});
    root.append(kpis);
    const grid=n('div','perf-grid'); const left=n('div'), right=n('div'); grid.append(left,right);
    const compare=box('Model development comparison','Same 2025 evaluation set throughout. Lower Brier score and log loss mean better probabilistic predictions; these differences are not guarantees of future improvement.');
    const tbl=n('table','perf-table');
    const head=n('thead'), header=n('tr'); ['Version','Brier ↓','Log loss ↓'].forEach(x=>header.append(n('th','',x))); head.append(header);tbl.append(head);
    const body=n('tbody');d.models.forEach((m,i)=>{const row=n('tr',i===d.models.length-1?'highlight':'');[m.name,metric(m.brier),metric(m.log_loss)].forEach(v=>row.append(n('td','',v)));body.append(row);});tbl.append(body);compare.append(tbl);
    compare.append(n('div','perf-note','The 2025 evaluation was revisited after each feature addition. That makes it useful for development comparisons but not a fresh independent test of the final model.'));
    left.append(compare);
    const chart=box('Brier score by version','All bars use the same axis, from 0 to 0.11. Lower is better.');
    d.models.forEach((m,i)=>{const row=n('div','perf-chart-row');row.append(n('span','',i===0?'Original':`Phase ${i+4}`));const track=n('div','perf-chart-track');const fill=n('div','perf-chart-bar'+(i===3?' featured':''));fill.style.width=`${Math.max(0,Math.min(100,m.brier/0.11*100))}%`;track.append(fill);row.append(track,n('span','perf-chart-num',m.brier.toFixed(5)));chart.append(row);});
    left.append(chart);
    const calibrate=box('Calibration: predicted vs. actual','Over all 7,011 evaluated player forecasts, our Phase 7 model slightly overestimated touchdown frequency on average.');
    const pair=n('div','perf-metric-pair');[[percent(d.average_phase7_probability),'Mean predicted TD chance'],[percent(d.actual_scoring_frequency),'Actual scoring frequency']].forEach(([v,label])=>{const item=n('div');item.append(n('strong','',v),n('span','',label));pair.append(item);});calibrate.append(pair);
    const high=d.high_probability_subset;
    calibrate.append(n('div','perf-note',`${high.n} higher-probability predictions (selected by Phase 6 ≥40%): Phase 7 averaged ${percent(high.phase7_average_probability)}; actual scoring was ${percent(high.actual_scoring_frequency)}. The subset was selected using a different model, so interpret carefully.`));
    right.append(calibrate);
    const status=box('2026 forward-testing record','A timestamp on an updated app proves the data refreshed—not whether a particular player scored or whether the predictions were accurate.');
    status.append(n('span','perf-pill','Separate forward log'));
    status.append(n('p','','Live pregame forecasts and postgame matching are tracked separately under the 2026 tracking tab. This section remains a historical development benchmark.'));
    right.append(status);
    const limitations=box('Known limitations','Important before the model is treated as a serious predictive tool.');
    [
      ['Players without current-season appearances','Players such as a previously productive veteran who has yet to play this season can be underrated. Future versions should use prior-year data and explicit availability modeling, not assume zero talent.'],
      ['Availability is not verified','Injury reports and missing stat rows do not prove a player is active or inactive. Official game-day inactives remain a separate check.'],
      ['Weather is still context only','Wind, precipitation, temperature, and stadium roof status are displayed but do not change Phase 7 probabilities.'],
      ['Postgame stat-row issue',`${d.postgame_row_audit.n_missing.toLocaleString('en-US')} forecasts lacked a same-week player stat row in the old audit. That is a postgame diagnostic, not a pregame input.`],
      ['No validated sportsbook advantage','No properly timestamped historical market prices were used to assess whether any apparent pricing differences represent genuine value.']
    ].forEach(([title,description])=>{const x=n('div','perf-limitation');x.append(n('strong','',title),n('p','',description));limitations.append(x);});
    right.append(limitations);root.append(grid);
  }
  fetch('./validation_2025.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error(`Could not load validation data (${r.status}).`);return r.json();}).then(d=>render(validate(d))).catch(e=>{
    const root=$('perf-data');root.replaceChildren();const x=box('Performance data unavailable','The dashboard has not changed. The validation JSON must be uploaded alongside this page.');x.append(n('p','',`Technical detail: ${e.message}`));root.append(x);
  });
})();
