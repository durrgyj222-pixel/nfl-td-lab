/* NFL TD Lab Phase 14: transparent forward record, no mock results. */
(() => {
  'use strict';
  const root = document.getElementById('track-root');
  const el = (tag, className, value) => {const node=document.createElement(tag);if(className)node.className=className;if(value!==undefined)node.textContent=String(value);return node;};
  const metric = (value, n=4) => value===null||value===undefined ? '—' : Number(value).toFixed(n);
  function valid(d){
    if(d?.schema_version!==1|| !Array.isArray(d.games) || !Number.isInteger(d.archived_games) || !Number.isInteger(d.graded_games) || !Number.isInteger(d.graded_player_rows))throw Error('Tracking JSON has invalid format');
    if(d.graded_player_rows && (!Number.isFinite(d.brier_on_graded_rows)||!Number.isFinite(d.scoring_rate_on_graded_rows)))throw Error('Invalid metric');
    return d;
  }
  function render(d){
    root.replaceChildren();
    const grid=el('div','track-kpis');
    const kpis=[[''+d.archived_games,'Pregame games archived'],[''+d.graded_games,'Finished games graded'],[''+d.graded_player_rows,'Matched player results'],[metric(d.brier_on_graded_rows,5),'Brier on matched rows ↓']];
    for(const [number,caption] of kpis){let item=el('div','track-kpi');item.append(el('strong','',number),el('span','',caption));grid.append(item);}root.append(grid);
    const note=el('div','track-note',d.important_limitation);root.append(note);
    const summary=el('section','track-panel');summary.append(el('h2','', 'What has been graded?'));
    if(!d.graded_games){
      summary.append(el('p','',d.archived_games ? `We have archived ${d.archived_games} future games, but no finished games have met our conservative grading requirements yet. Check again after results and weekly player stats publish.` : 'No verified pregame forecast snapshots have been archived yet. GitHub will begin recording them when Phase 14 runs before kickoff.'));
    } else {
      summary.append(el('p','',`Among ${d.graded_player_rows} matched, non-excluded player records, the observed touchdown rate was ${(100*d.scoring_rate_on_graded_rows).toFixed(1)}%. Brier score ${(d.brier_on_graded_rows).toFixed(5)}. These are conditional on player stat-row availability, not complete-slate accuracy.`));
    }
    root.append(summary);
    if(d.games.length){
      const panel=el('section','track-panel');panel.append(el('h2','', 'Recently graded matchups'));
      const wrap=el('div','track-table-wrap'), table=el('table','track-table');
      const head=el('thead'), tr=el('tr');['Game','Week','Graded players','TD scorers','Brier ↓'].forEach(x=>tr.append(el('th','',x)));head.append(tr);table.append(head);
      const body=el('tbody');
      for(const game of d.games){
        let row=el('tr');[game.game,`${game.season} • ${game.week}`, game.graded_players, game.scorers,metric(game.brier_scored_rows,4)].forEach(v=>row.append(el('td','',v)));
        body.append(row);
      }
      table.append(body);wrap.append(table);panel.append(wrap);root.append(panel);
    }
    root.append(el('p','track-footer',`Last generated summary: ${new Date(d.generated_at_utc).toLocaleString()}. Historical records are unchanged when the live matchup dashboard refreshes.`));
  }
  fetch('./forward_latest.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error(`HTTP ${r.status}`);return r.json();}).then(d=>render(valid(d))).catch(e=>{
    root.replaceChildren();const box=el('section','track-panel');box.append(el('h2','', 'Forward record not available yet'),el('p','',`The tracking file has not been published or is invalid (${e.message}). The matchup dashboard is unaffected.`));root.append(box);
  });
})();
