const $ = id => document.getElementById(id);
let current = null;
let busy = false;
const serviceStyles = {
  argocd: {accent: "mint", icon: "↗", category: "Control plane"},
  "home-assistant": {accent: "blue", icon: "⌂", category: "Home automation"},
  authentik: {accent: "coral", icon: "◎", category: "Identity"},
  scruffy: {accent: "pink", icon: "▦", category: "Infrastructure"},
  roberto: {accent: "pink", icon: "▦", category: "Infrastructure"},
  grafana: {accent: "orange", icon: "◴", category: "Monitoring"},
};
const date = value => value ? new Date(value).toLocaleString("nl-BE") : "Nog niet gesynchroniseerd";
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function badge(text) {
  const good = ["healthy","reachable","Healthy","Synced"].includes(text);
  const bad = ["unhealthy","unreachable","Degraded","Missing"].includes(text);
  const labels = {healthy:"Healthcheck OK",reachable:"Bereikbaar",unhealthy:"Check mislukt",unreachable:"Onbereikbaar"};
  return el("span",labels[text] || text,"badge "+(good ? "good" : bad ? "bad" : "neutral"));
}
function link(text, url) {
  const node = el("a",text); node.href = url; node.target = "_blank"; node.rel = "noopener noreferrer"; return node;
}
async function detail(service) {
  $("detail-title").textContent = service.name + " · recente checks";
  $("detail-content").replaceChildren(el("p","Laden…"));
  $("detail").showModal();
  try {
    const response = await fetch("/api/services/"+encodeURIComponent(service.id));
    if (!response.ok) throw new Error();
    const data = await response.json();
    $("detail-content").replaceChildren(...data.history.slice().reverse().map(item => {
      const row=el("div",undefined,"history-row");
      row.append(el("span",date(item.checked_at)),badge(item.status),el("span",item.latency_ms+" ms"));
      return row;
    }));
  } catch { $("detail-content").replaceChildren(el("p","Geschiedenis kon niet geladen worden.")); }
}
function renderNodes(data) {
  const stale = !data.nodes_checked_at || Date.now()-Date.parse(data.nodes_checked_at)>data.interval_seconds*3000;
  const unavailable = stale || Boolean(data.nodes_error);
  $("nodes-error").hidden = !unavailable;
  $("nodes-error").textContent = data.nodes_error || "Geen recente nodemeting; gegevens kunnen verouderd zijn.";
  $("nodes-updated").textContent = data.nodes_checked_at ? "Meting: "+date(data.nodes_checked_at)+(unavailable ? " · verouderd" : "") : "";
  const cards = (data.nodes || []).map(node => {
    const card = el("article",undefined,"node-card"+(unavailable ? " stale" : ""));
    const heading = el("div",undefined,"row");
    const name = el("h3",node.name.replace(/^k3s-\d+-/,"")); name.title=node.name;
    heading.append(name,el("span",unavailable || node.ready == null ? "Onbekend" : node.ready ? "Ready" : "Not ready","badge "+(unavailable || node.ready == null ? "neutral" : node.ready ? "good" : "bad")));
    card.append(heading);
    for(const [label,key] of [["CPU","cpu_percent"],["RAM","memory_percent"]]) {
      const value=node[key];
      const row=el("div",undefined,"node-metric");
      row.append(el("span",label),el("strong",value == null ? "—" : value.toFixed(1)+"%"));
      if(value != null) {
        const meter=el("meter");meter.min=0;meter.max=100;meter.low=75;meter.high=90;meter.optimum=0;meter.value=value;
        meter.setAttribute("aria-label",node.name+" "+label+(unavailable ? " (verouderd)" : ""));row.append(meter);
      }
      card.append(row);
    }
    return card;
  });
  $("nodes").replaceChildren(...(cards.length ? cards : [el("p","Geen nodemetingen beschikbaar.","muted")]));
}
function renderServices(data) {
  $("services").replaceChildren(...data.services.map(service => {
    const style=serviceStyles[service.id] || {accent:"gold",icon:"✦",category:"Service"};
    const card=el("article",undefined,"card accent-"+style.accent);
    const heading=el("div",undefined,"card-heading");
    const icon=el("span",style.icon,"app-icon");icon.setAttribute("aria-hidden","true");
    const title=el("div");
    title.append(el("p",style.category,"app-category"),el("h3",service.name));
    heading.append(icon,title);card.append(heading);
    const row=el("div",undefined,"row");
    row.append(badge(service.status),link("Open ↗",service.url));card.append(row);
    card.append(el("p",service.latency_ms+" ms","latency"));
    card.append(el("p",service.probe==="health"?"Expliciete healthcheck":"HTTP-bereikbaarheid, geen interne healthcheck","muted"));
    if(service.error) card.append(el("p",service.error,"muted"));
    if(service.http_status) card.append(el("p","HTTP "+service.http_status,"muted"));
    const button=el("button","Recente controles");button.addEventListener("click",()=>detail(service));card.append(button);
    return card;
  }));
}
function renderDeployments() {
  const apps=(current?.deployments || []).filter(app=>app.name.toLowerCase().includes($("search").value.toLowerCase()));
  if(!apps.length) {
    const row=el("tr"),cell=el("td","Geen applicaties gevonden.");cell.colSpan=6;row.append(cell);$("deployments").replaceChildren(row);return;
  }
  $("deployments").replaceChildren(...apps.map(app=>{
    const row=el("tr"),name=el("td"),health=el("td"),sync=el("td"),revisions=el("td"),last=el("td"),actions=el("td");
    name.append(el("strong",app.name),el("small",app.namespace || "—"));
    health.append(badge(app.health));sync.append(badge(app.sync));
    for(const source of app.sources) {
      const label=source.chart ? source.chart+" · "+(source.revision || source.desired || "?") : (source.revision?.slice(0,12) || "Onbekend");
      const item=source.commit_url ? link(label,source.commit_url) : el("span",label);
      item.className="revision";revisions.append(item);
    }
    last.append(el("span",date(app.last_sync)),el("small",app.operation));
    actions.append(link("Argo ↗",app.argocd_url));
    row.append(name,health,sync,revisions,last,actions);return row;
  }));
}
async function load() {
  if(busy)return;busy=true;
  try {
    const response=await fetch("/api/status");
    if(!response.ok)throw new Error();
    current=await response.json();
    const stale=!current.checked_at || Date.now()-Date.parse(current.checked_at)>current.interval_seconds*3000;
    $("error").hidden=!stale;
    $("error").textContent="Nog geen recente meting. Getoonde gegevens kunnen verouderd zijn.";
    $("updated").textContent=current.checked_at ? "Laatste controle: "+date(current.checked_at)+" · elke "+current.interval_seconds+" s" : "Eerste controle loopt…";
    $("deployment-error").hidden=!current.deployments_error;
    $("deployment-error").textContent=current.deployments_error || "";
    $("deployment-updated").textContent=current.deployments_checked_at ? "Deploymentgegevens van "+date(current.deployments_checked_at) : "Nog geen deploymentmeting";
    $("trace").textContent=current.trace_id ? "Refresh trace: "+current.trace_id : "Trace-export nog niet aangesloten";
    renderNodes(current);renderServices(current);renderDeployments();
  }catch {
    $("error").hidden=false;$("error").textContent="Dashboard-API niet bereikbaar. Eventuele eerdere gegevens zijn niet actueel.";
  }finally{busy=false;}
}
$("refresh").addEventListener("click",load);
$("search").addEventListener("input",renderDeployments);
$("close").addEventListener("click",()=>$("detail").close());
load();setInterval(load,10000);
