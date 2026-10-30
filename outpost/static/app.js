// Small progressive enhancements; every page works without this file.
(function () {
  // Contest clock. The server's time is sent with the page so a contestant's
  // laptop clock being off doesn't matter.
  var timer = document.querySelector("[data-timer]");
  if (timer) {
    var skew = Date.now() / 1000 - parseFloat(timer.dataset.now);
    var start = parseFloat(timer.dataset.start), end = parseFloat(timer.dataset.end);
    var fmt = function (s) {
      s = Math.max(0, Math.floor(s));
      var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
      return h + ":" + String(m).padStart(2, "0") + ":" + String(x).padStart(2, "0");
    };
    var tick = function () {
      var now = Date.now() / 1000 - skew;
      if (now < start) { timer.textContent = "starts in " + fmt(start - now); }
      else if (now < end) { timer.textContent = fmt(end - now) + " left"; }
      else { timer.textContent = "contest over"; timer.classList.add("over"); }
      if (timer.dataset.reloadAtStart && now >= start && now < start + 2) location.reload();
    };
    tick(); setInterval(tick, 1000);
  }

  // Copy buttons on samples.
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = document.getElementById(btn.dataset.copy).textContent;
      var done = function () { btn.textContent = "copied"; setTimeout(function () { btn.textContent = "copy"; }, 1200); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
      else { var t = document.createElement("textarea"); t.value = text; document.body.appendChild(t);
             t.select(); document.execCommand("copy"); t.remove(); done(); }
    });
  });

  // Tab inserts spaces in the code box instead of leaving it.
  document.querySelectorAll("textarea.code").forEach(function (ta) {
    ta.addEventListener("keydown", function (e) {
      if (e.key !== "Tab") return;
      e.preventDefault();
      var s = ta.selectionStart;
      ta.value = ta.value.slice(0, s) + "    " + ta.value.slice(ta.selectionEnd);
      ta.selectionStart = ta.selectionEnd = s + 4;
    });
  });

  // Live verdicts on "My submissions".
  var subs = document.querySelector("[data-poll-submissions]");
  if (subs) {
    var poll = function () {
      fetch(subs.dataset.pollSubmissions, { credentials: "same-origin" }).then(function (r) { return r.json(); })
        .then(function (rows) {
          var waiting = false;
          rows.forEach(function (r) {
            var tr = document.getElementById("sub-" + r.id);
            if (!tr) return;
            if (r.status !== "done") { waiting = true; return; }
            if (tr.dataset.status === "done") return;
            tr.dataset.status = "done";
            var v = tr.querySelector(".v");
            v.className = "v " + r.verdict;
            v.textContent = r.verdict_label + (r.failed_test && r.verdict !== "accepted" && subs.dataset.icpc
              ? " on test " + r.failed_test : "");
            var sc = tr.querySelector(".score");
            if (sc && r.score !== null) sc.textContent = (+r.score) + " / " + (+r.max_score);
            tr.querySelector(".time").textContent = r.time_ms + " ms";
            tr.querySelector(".mem").textContent = Math.round(r.memory_kb / 1024) + " MB";
          });
          if (waiting) setTimeout(poll, 1500);
        }).catch(function () { setTimeout(poll, 4000); });
    };
    if (subs.querySelector("tr[data-status='pending'], tr[data-status='judging']")) setTimeout(poll, 1000);
  }

  // Announcement / answer badge.
  var news = document.querySelector("[data-news]");
  if (news) {
    var key = "outpost-seen-" + news.dataset.news;
    var check = function () {
      fetch(news.dataset.news, { credentials: "same-origin" }).then(function (r) { return r.json(); })
        .then(function (d) {
          var seen = 0;
          try { seen = parseFloat(localStorage.getItem(key) || "0"); } catch (e) {}
          if (location.pathname.indexOf("/clarifications") >= 0) {
            try { localStorage.setItem(key, String(d.latest)); } catch (e) {}
            seen = d.latest;
          }
          news.hidden = !(d.latest > seen);
        }).catch(function () {});
    };
    check(); setInterval(check, 15000);
  }

  // Math in statements (KaTeX is vendored; nothing is fetched from the internet).
  if (window.renderMathInElement) {
    document.querySelectorAll(".statement").forEach(function (el) {
      window.renderMathInElement(el, {
        delimiters: [{ left: "\\[", right: "\\]", display: true }, { left: "\\(", right: "\\)", display: false }],
        throwOnError: false,
      });
    });
  }

  // Confirm dangerous buttons.
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) { if (!confirm(el.dataset.confirm)) e.preventDefault(); });
  });
})();
