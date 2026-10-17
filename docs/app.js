/* NFL TD Lab Phase 10 — static, read-only, pregame snapshot dashboard.
   No API keys, live requests, or data are stored in the page itself.
   The generated docs/data.json is the only data source. */
(() => {
  'use strict';
  let report = null;
  let selected = null;
  let query = '';
  let showAll = false;
  let showExcluded = false;
  const $ = (id) => document.getElementById(id);
  const el = (tag, className, textValue) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (textValue !== undefined && textValue !== null) node.textContent = String(textValue);
    return node;
  };
  function etTime(iso, opts = {}) {
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return 'Kickoff time unavailable';
    return new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York', ...opts}).format(d);
  }
  function num(value, unit = '', fallback = '—') {
    if (value === null || value === undefined || String(value).trim() === '') return fallback;
    const n = Number(value);
    return Number.isFinite(n) ? `${Math.round(n)}${unit}` : fallback;
  }
  function gameTime(game) {
    return `${etTime(game.kickoff_utc,{weekday:'short',month:'short',day:'numeric'})} · ${etTime(game.kickoff_utc,{hour:'numeric',minute:'2-digit'})} ET`;
  }
  const isOut = (p) => String(p?.status || '').startsWith('EXCLUDED:');
  function eligible(game) {
    const all = Array.isArray(game.all_players) ? game.all_players : [];
    return (all.length ? all.filter(p => !isOut(p)) : (game.favorites || []))
      .slice().sort((a,b) => (Number(b.td_probability) || 0) - (Number(a.td_probability) || 0));
  }
  function flags(game) {
    return String(game.weather?.weather_flags || 'WEATHER NOT AVAILABLE').trim() || 'WEATHER NOT AVAILABLE';
  }
  function isAlert(txt) {
    return /(STRONG|HEAVY|SNOW|FREEZ|EXTREME|GUSTS|CHECK ROOF|HIGH WIND|ROOF UNCONFIRMED)/i.test(txt);
  }
  function setText(id, value) { $(id).textContent = String(value); }
  function renderHeader() {
    const games = report.games;
    const first = games[0];
    const year = first && /^\d{4}/.test(first.kickoff_utc || '') ? String(first.kickoff_utc).slice(0,4) : 'NFL';
    setText('season-week', `${year} / WEEK ${first?.week ?? '—'}`);
    setText('game-count',games.length);
    setText('player-count',games.reduce((n,g) => n + (Array.isArray(g.all_players) ? g.all_players.length : (g.favorites || []).length),0));
    setText('warning-count',games.reduce((n,g) => n + (g.all_players || g.favorites || []).filter(p => isOut(p) || String(p.status || '').startsWith('UNCERTAIN')).length,0));
    const dt = new Date(report.generated_at_utc);
    const isValid = Number.isFinite(dt.getTime());
    const ageHours = isValid ? (Date.now() - dt.getTime()) / 3600000 : 0;
    const stamp = isValid ? `${etTime(report.generated_at_utc,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})} ET` : 'unknown time';
    setText('freshness', `Saved ${stamp}. ${ageHours > 24 ? 'This snapshot is over 24 hours old. Rebuild before relying on it.' : 'Refresh by rerunning Phase 9 and Phase 10 on your computer.'} Weather and injury information can change.`);
  }
  function renderList() {
    const list = $('game-list');
    list.replaceChildren();
    const filtered = report.games.filter(g => [g.game,...(g.all_players || []).map(p => p.name), ...(g.favorites || []).map(p => p.name)]
      .some(s => String(s || '').toLowerCase().includes(query)));
    setText('list-count',filtered.length);
    if (filtered.length && !filtered.some(g => g.game === selected)) {
      selected = filtered[0].game;
      showAll = false; showExcluded = false;
    }
    if (!filtered.length) { list.append(el('div','empty','No games or players match that search.')); $('detail').replaceChildren(el('div','empty','Try another search.')); return; }
    filtered.forEach(g => {
      const button=el('button','game-choice' + (g.game === selected ? ' selected' : ''));
      button.type='button';
      button.setAttribute('aria-pressed', g.game === selected ? 'true' : 'false');
      button.setAttribute('aria-label',`Open ${g.game}, ${gameTime(g)}`);
      const first=el('div','choice-row');
      first.append(el('span','choice-title',g.game));
      first.append(el('span','choice-date',etTime(g.kickoff_utc,{weekday:'short',hour:'numeric',minute:'2-digit'})));
      const weather=el('div','choice-weather' + (isAlert(flags(g)) ? ' choice-alert' : ''),flags(g));
      button.append(first,weather);
      button.addEventListener('click',()=>{selected=g.game;showAll=false;showExcluded=false;renderList();renderDetail();});
      list.append(button);
    });
    renderDetail();
  }
  function infoPill(text, special = false) { return el('div','weather-pill' + (special ? ' special' : ''),text); }
  function renderPlayer(p,i) {
    const row = el('article','player-row');
    const main=el('div','player-main');
    const identity=el('div','player-identity');
    identity.append(el('div','rank',i + 1));
    const details=el('div');
    details.append(el('div','player-name',p.name || 'Unknown player'),el('div','player-meta',[p.team,p.position].filter(Boolean).join(' · ')));
    identity.append(details);
    const score=el('div','score');
    const raw=Number(p.td_probability);
    const val=Number.isFinite(raw) ? Math.max(0,Math.min(100,raw*100)) : null;
    score.append(el('span','',val === null ? '—' : `${val.toFixed(1)}%`),el('small','','MODEL TD CHANCE'));
    main.append(identity,score);
    const track=el('div','prob-track');
    const fill=el('div','prob-fill'); fill.style.width=`${val ?? 0}%`; track.append(fill);
    const foot=el('div','row-foot');
    const statusText=String(p.status || 'UNKNOWN / NOT VERIFIED');
    const status=el('span','status' + (statusText.startsWith('UNCERTAIN') ? ' uncertain' : ''),statusText);
    foot.append(status);
    const usages=[];
    if (p.inside5_carries_last4 !== null && p.inside5_carries_last4 !== undefined && p.inside5_carries_last4 !== '') usages.push(`Inside-5 carries: ${p.inside5_carries_last4}`);
    if (p.red_zone_targets_last4 !== null && p.red_zone_targets_last4 !== undefined && p.red_zone_targets_last4 !== '') usages.push(`RZ targets: ${p.red_zone_targets_last4}`);
    if (usages.length) foot.append(el('span','usage',usages.join('  ·  ')));
    row.append(main,track,foot);
    return row;
  }
  function renderDetail() {
    const game=report.games.find(g=>g.game===selected);
    if (!game) return;
    const detail=$('detail'); detail.replaceChildren();
    const head=el('div','match-head');
    const sup=el('div','match-suptitle');
    sup.append(el('span','',`WEEK ${game.week ?? '—'} / MATCHUP PROFILE`),el('span','snapshot-tag','SAVED SNAPSHOT'));
    head.append(sup);
    const match=/^(.+?)\s+@\s+(.+)$/.exec(String(game.game || ''));
    const away=match ? match[1]:'AWAY', home=match ? match[2]:'HOME';
    const teams=el('div','match-teams');
    function teamTile(code,label) {const block=el('div','team-block'); block.append(el('div','team-tile',code),el('div','team-short',label));return block;}
    teams.append(teamTile(away,'AWAY'),el('div','at','AT'),teamTile(home,'HOME'));
    head.append(teams,el('div','kickoff',gameTime(game)));
    detail.append(head);
    const w=game.weather || {};
    const weather=el('div','weather-section');
    weather.append(el('div','section-mini','GAMEDAY CONDITIONS'));
    const pills=el('div','weather-wrap');
    if (w.stadium) pills.append(infoPill(String(w.stadium)));
    if (w.roof) pills.append(infoPill(String(w.roof),/UNCONFIRMED/i.test(String(w.roof))));
    if (w.temperature_f !== '' && w.temperature_f != null) pills.append(infoPill(`${num(w.temperature_f,'°F')} temp`));
    if (w.wind_mph_max !== '' && w.wind_mph_max != null) pills.append(infoPill(`${num(w.wind_mph_max,' mph')} wind`));
    if (w.gust_mph_max !== '' && w.gust_mph_max != null) pills.append(infoPill(`${num(w.gust_mph_max,' mph')} gusts`));
    if (w.precip_probability_pct_max !== '' && w.precip_probability_pct_max != null) pills.append(infoPill(`${num(w.precip_probability_pct_max,'%')} rain chance`));
    if (!pills.childNodes.length) pills.append(infoPill('Forecast unavailable'));
    weather.append(pills,el('div','weather-flag' + (isAlert(flags(game)) ? ' alert' : ''),flags(game)+' · Weather context only; probabilities are unchanged.'));
    detail.append(weather);
    const section=el('section','player-section');
    const sHead=el('div','section-header');
    sHead.append(el('h2','','Touchdown candidates'),el('span','','PHASE 7 MODEL'));
    section.append(sHead,el('p','section-subtext','Ranked by experimental rushing/receiving TD probability. Not injury-adjusted; not a market-value ranking.'));
    const candidates=eligible(game);
    const rows=el('div','player-list');
    candidates.slice(0,showAll ? undefined : 5).forEach((p,i)=>rows.append(renderPlayer(p,i)));
    if (!candidates.length) rows.append(el('div','empty','No eligible player projections in this saved snapshot.'));
    section.append(rows);
    if (candidates.length>5){
      const more=el('button','toggle',showAll ? 'Show top 5 only' : `Show all ${candidates.length} player projections`);
      more.type='button'; more.setAttribute('aria-expanded',showAll?'true':'false');
      more.addEventListener('click',()=>{showAll=!showAll;renderDetail();});
      const holder=el('div','extra-section'); holder.append(more); section.append(holder);
    }
    const excluded=Array.isArray(game.excluded) ? game.excluded : [];
    if(excluded.length){
      const box=el('div','excluded-box');
      box.append(el('h3','excluded-title',`Reported out / excluded (${excluded.length})`));
      const toggle=el('button','toggle',showExcluded?'Hide excluded players':'Review excluded players');
      toggle.type='button';toggle.setAttribute('aria-expanded',showExcluded?'true':'false');
      toggle.addEventListener('click',()=>{showExcluded=!showExcluded;renderDetail();});box.append(toggle);
      if(showExcluded) excluded.forEach(p=>{const r=el('div','excluded-row');r.append(el('span','',`${p.name||'Unknown'} · ${p.team||''}`),el('span','status excluded',p.reported_game_status||'OUT'));box.append(r);});
      section.append(box);
    }
    detail.append(section);
  }
  async function init() {
    try {
      const response=await fetch(`data.json`,{cache:'no-store'});
      if (!response.ok) throw Error(`data.json unavailable (HTTP ${response.status})`);
      const data=await response.json();
      if (!data || data.app_data_version!==1 || !Array.isArray(data.games) || !data.games.length) throw Error('The JSON has no valid Phase 9 matchup data.');
      report=data;
      renderHeader();
      const upcoming=data.games.filter(g=>Number.isFinite(new Date(g.kickoff_utc).getTime()) && new Date(g.kickoff_utc).getTime()>Date.now());
      selected=(upcoming[0]||data.games[0]).game;
      $('game-search').addEventListener('input',e=>{query=e.target.value.toLowerCase().trim();renderList();});
      renderList();
    } catch (err) {
      setText('freshness','Could not load predictions. Run the Phase 10 build command, then use the local preview server.');
      $('game-list').replaceChildren(el('div','empty','No saved matchup data.'));
      $('detail').replaceChildren(el('div','empty',`Dashboard could not open data.json: ${err.message}. Opening index.html directly from your Downloads folder will not work; use the preview command.`));
    }
  }
  init();
})();
