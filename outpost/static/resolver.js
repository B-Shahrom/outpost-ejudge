// ICPC resolver: reveal the runs the freeze hid, bottom of the board first.
// The server sends the frozen board and the final result of each hidden cell;
// everything else happens here.
(function () {
  var root = document.getElementById("resolver");
  if (!root) return;
  var status = document.getElementById("resolver-status");
  var data, rows, probs, focus = null, auto = null;

  function cmp(a, b) {
    return (b.solved - a.solved) || (a.penalty - b.penalty) || a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  }
  function hidden(r) {  // the row's leftmost problem still hidden, if any
    var rev = data.reveal[r.id] || {};
    for (var i = 0; i < probs.length; i++) {
      if (rev[probs[i].id] && r.cells[probs[i].id].pending) return probs[i].id;
    }
    return null;
  }
  function target() {
    for (var i = rows.length - 1; i >= 0; i--) {
      var pid = hidden(rows[i]);
      if (pid) return { row: rows[i], pid: pid };
    }
    return null;
  }
  function cellHtml(r, p) {
    var c = r.cells[p.id], cls = "", text = "";
    if (c.solved) { cls = c.first ? "cell-first" : "cell-ok"; text = "+" + (c.tries || "") + '<span class="t">' + c.minute + "</span>"; }
    else if (c.pending) { cls = "cell-pending"; text = "?" + (c.tries ? '<span class="t">−' + c.tries + " +" + c.pending + "</span>" : ""); }
    else if (c.tries) { cls = "cell-bad"; text = "−" + c.tries; }
    if (focus && focus.row === r && focus.pid === p.id) cls += " cell-focus";
    return '<td class="' + cls + '">' + text + "</td>";
  }
  function render() {
    var before = {};
    root.querySelectorAll("tr[data-id]").forEach(function (tr) { before[tr.dataset.id] = tr.getBoundingClientRect().top; });
    rows.sort(cmp);
    var html = '<table class="board"><tr><th>#</th><th class="name">Name</th>';
    probs.forEach(function (p) { html += '<th title="' + p.name.replace(/"/g, "&quot;") + '">' + p.label + "</th>"; });
    html += "<th>Solved</th><th>Penalty</th></tr>";
    var rank = 0;
    rows.forEach(function (r, n) {
      // Equal solved and penalty share a place (names only order the rows).
      if (n === 0 || rows[n - 1].solved !== r.solved || rows[n - 1].penalty !== r.penalty) rank = n + 1;
      html += '<tr data-id="' + r.id + '"' + (focus && focus.row === r ? ' class="resolving"' : "") + "><td>" + rank +
        '</td><td class="name">' + r.name.replace(/</g, "&lt;") + "</td>";
      probs.forEach(function (p) { html += cellHtml(r, p); });
      html += "<td><b>" + r.solved + "</b></td><td>" + r.penalty + "</td></tr>";
    });
    root.innerHTML = html + "</table>";
    // FLIP: start each row where it was, then let it slide to its new place.
    root.querySelectorAll("tr[data-id]").forEach(function (tr) {
      if (!(tr.dataset.id in before)) return;
      var dy = before[tr.dataset.id] - tr.getBoundingClientRect().top;
      if (!dy) return;
      tr.style.transform = "translateY(" + dy + "px)";
      tr.style.transition = "none";
      requestAnimationFrame(function () {
        tr.style.transition = "transform 700ms ease";
        tr.style.transform = "";
      });
    });
    if (focus) {
      var row = root.querySelector("tr.resolving");
      if (row && row.scrollIntoView) row.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  function step() {
    if (!focus) {
      focus = target();
      if (!focus) { finish(); return; }
      render();
      return;
    }
    var f = data.reveal[focus.row.id][focus.pid], c = focus.row.cells[focus.pid];
    c.pending = 0;
    c.tries = f.tries;
    if (f.solved) {
      c.solved = true; c.minute = f.minute; c.first = f.first;
      focus.row.solved += 1;
      focus.row.penalty += f.penalty;
    }
    focus = null;
    render();
    if (!target()) finish();
  }
  function finish() {
    if (auto) { clearInterval(auto); auto = null; }
    status.textContent = "final standings";
    status.className = "pill ok";
    root.dataset.done = "1";
  }
  fetch(root.dataset.src, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
    data = d; rows = d.rows; probs = d.problems;
    render();
    if (!target()) finish();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === " " || e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); step(); }
    if (e.key === "a" || e.key === "A") {
      if (auto) { clearInterval(auto); auto = null; } else { auto = setInterval(step, 900); }
    }
  });
  root.addEventListener("click", step);
  window.outpostResolverStep = step;  // for tests
})();
