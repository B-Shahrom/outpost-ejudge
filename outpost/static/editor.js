// The code box on a problem page, as an editor (vendored CodeMirror 5).
// Without JavaScript the plain textarea still works; the form posts the same field.
(function () {
  var ta = document.querySelector("textarea[data-editor]");
  if (!ta || !window.CodeMirror) return;
  var select = document.getElementById(ta.dataset.languageSelect);
  var modes = { cpp: "text/x-c++src", java21: "text/x-java", py12: "python" };
  var key = ta.dataset.draftKey;
  // A draft per problem survives a reload or a closed tab (this browser only).
  try { if (!ta.value && key) ta.value = localStorage.getItem(key) || ""; } catch (e) {}
  var cm = CodeMirror.fromTextArea(ta, {
    lineNumbers: true, indentUnit: 4, tabSize: 4, indentWithTabs: false,
    matchBrackets: true, autoCloseBrackets: true, viewportMargin: Infinity,
    lineWrapping: true,  // the side panel is narrow: wrap rather than scroll sideways
    mode: modes[select && select.value] || modes.cpp,
    extraKeys: {
      Tab: function (cm) {
        if (cm.somethingSelected()) cm.indentSelection("add");
        else cm.replaceSelection("    ", "end");
      },
      "Shift-Tab": function (cm) { cm.indentSelection("subtract"); },
    },
  });
  if (select) select.addEventListener("change", function () { cm.setOption("mode", modes[select.value] || modes.cpp); });
  cm.on("change", function () { try { localStorage.setItem(key, cm.getValue()); } catch (e) {} });
  ta.form.addEventListener("submit", function () { cm.save(); });
  window.outpostEditor = cm;  // for tests
})();
