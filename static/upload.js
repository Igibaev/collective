// Загрузка файлов частями: каждый кусок — отдельный небольшой запрос,
// поэтому лимит Cloudflare на размер запроса (100 МБ) не мешает.
(function () {
  var form = document.getElementById('claimform');
  if (!form || !window.fetch || !window.Blob || !Blob.prototype.slice) return;
  var csrf = form.querySelector('input[name="_csrf"]').value;
  var warn = document.getElementById('uplwarn');
  var active = 0;
  var queue = Promise.resolve();

  function mb(n) { return (n / 1048576).toFixed(1) + ' МБ'; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function post(url, body, json) {
    var h = { 'X-CSRF-Token': csrf };
    if (json) h['Content-Type'] = 'application/json';
    return fetch(url, { method: 'POST', headers: h, body: json ? JSON.stringify(body) : body, credentials: 'same-origin' });
  }
  function setStatus(li, text, cls) {
    var st = li.querySelector('.st');
    st.textContent = text; st.className = 'st ' + (cls || '');
  }
  function addRemove(li) {
    var b = li.querySelector('.rm');
    b.addEventListener('click', function () {
      var t = li.getAttribute('data-token');
      if (t) post('/upload/delete/' + t, null, false);
      li.remove();
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll('.flist li'), addRemove);

  async function uploadFile(cat, file, li) {
    active++; updateWarn();
    try {
      var r = await post('/upload/start', { cat: cat, name: file.name, size: file.size }, true);
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) { setStatus(li, '✗ ' + (d.error || ('ошибка ' + r.status)), 'bad'); return; }
      li.setAttribute('data-token', d.token);
      var chunk = d.chunk, off = 0, tries = 0;
      while (off < file.size) {
        var part = file.slice(off, Math.min(off + chunk, file.size));
        var resp;
        try {
          resp = await post('/upload/chunk/' + d.token + '?offset=' + off, part, false);
        } catch (e) { resp = null; }
        if (resp && resp.ok) {
          var j = await resp.json(); off = j.received; tries = 0;
          setStatus(li, Math.floor(off * 100 / file.size) + '%', '');
        } else if (resp && resp.status === 409) {
          off = (await resp.json()).received;
        } else {
          if (resp && resp.status < 500 && resp.status !== 429) {
            var e = await resp.json().catch(function () { return {}; });
            setStatus(li, '✗ ' + (e.error || ('ошибка ' + resp.status)), 'bad'); return;
          }
          if (++tries > 6) { setStatus(li, '✗ связь оборвалась, удалите и выберите файл снова', 'bad'); return; }
          setStatus(li, 'нет связи, повтор ' + tries + '…', '');
          await sleep(2000 * tries);
        }
      }
      setStatus(li, '✓ загружен', 'ok');
    } finally { active--; updateWarn(); }
  }

  function updateWarn() {
    if (active > 0) { warn.hidden = false; warn.textContent = 'Файлы ещё загружаются (' + active + '). Не закрывайте страницу и подождите.'; }
    else { warn.hidden = true; }
  }

  Array.prototype.forEach.call(document.querySelectorAll('.upl'), function (box) {
    var cat = box.getAttribute('data-cat');
    var input = box.querySelector('input[type=file]');
    var list = box.querySelector('.flist');
    input.addEventListener('change', function () {
      var files = Array.prototype.slice.call(input.files);
      input.value = '';
      files.forEach(function (f) {
        var li = document.createElement('li');
        li.innerHTML = '<span class="fn"></span> <span class="fs"></span> <span class="st">в очереди</span> <button type="button" class="link rm">удалить</button>';
        li.querySelector('.fn').textContent = f.name;
        li.querySelector('.fs').textContent = mb(f.size);
        list.appendChild(li); addRemove(li);
        queue = queue.then(function () { return uploadFile(cat, f, li); });
      });
    });
  });

  form.addEventListener('submit', function (ev) {
    if (active > 0) { ev.preventDefault(); updateWarn(); warn.scrollIntoView({ block: 'center' }); return; }
    var bad = form.querySelectorAll('.flist .st.bad');
    if (bad.length) {
      ev.preventDefault(); warn.hidden = false;
      warn.textContent = 'Некоторые файлы не загрузились (отмечены ✗). Удалите их или выберите заново.';
      warn.scrollIntoView({ block: 'center' });
    }
  });
})();
