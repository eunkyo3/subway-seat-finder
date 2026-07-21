/* ============================================================
   서울 지하철 실시간 혼잡 예측 — 프런트엔드 (vanilla, no build)
   백엔드 계약은 고정. 이 파일은 읽기만 한다.
   ============================================================ */
(function () {
  'use strict';

  /* ---------------------------------------------------------
     0. 상수
     --------------------------------------------------------- */

  var REFRESH_SEC = 30;
  var RING_LEN = 2 * Math.PI * 15;      // r=15 원둘레
  var GAUGE_MAX = 200;                  // 게이지 눈금 상한(%)
  var DEFAULT_LINE = '2호선';
  var KEY_URL = 'https://data.seoul.go.kr/together/mypage/actkeyMain.do';

  // 공식 노선 색. 없는 노선은 회색으로 떨어진다.
  var LINE_COLORS = {
    '1호선': '#0052A4', '2호선': '#00A84D', '3호선': '#EF7C1C', '4호선': '#00A5DE',
    '5호선': '#996CAC', '6호선': '#CD7C2F', '7호선': '#747F00', '8호선': '#E6186C',
    '9호선': '#BB8336',
    'GTXA': '#9A6292', '경강선': '#003DA5', '경의중앙선': '#77C4A3', '경춘선': '#0C8E72',
    '공항철도': '#0090D2', '김포골드라인': '#A17800', '서해선': '#8FC31F',
    '수인분당선': '#F5A200', '신림선': '#6789CA', '신분당선': '#D4003B',
    '용인경전철': '#56AE4D', '우이신설선': '#B7C452', '의정부경전철': '#FDA600',
    '인천1호선': '#7CA8D5', '인천2호선': '#ED8B00'
  };
  var LINE_FALLBACK = '#6B7684';

  // 등급 색은 테마별로 다르다(대비 확보).
  var GRADE_COLORS = {
    '여유':     { light: '#12A150', dark: '#2FBF6B' },
    '보통':     { light: '#C08000', dark: '#E0A21B' },
    '혼잡':     { light: '#E2590C', dark: '#F2712C' },
    '매우혼잡': { light: '#D42A20', dark: '#F0524A' }
  };
  var NEUTRAL = { light: '#8494A6', dark: '#6E7E90' };

  var VERDICT_LABEL = {
    take_this: '이번 열차 탑승',
    take_next: '다음 열차 권장',
    similar:   '차이 미미'
  };

  /* ---------------------------------------------------------
     1. 유틸
     --------------------------------------------------------- */

  function $(sel) { return document.querySelector(sel); }

  /** 안전한 DOM 빌더. 사용자/서버 문자열은 항상 textNode 로 들어간다. */
  function h(tag, props) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'html') node.innerHTML = v;
        else if (k === 'on') Object.keys(v).forEach(function (e) { node.addEventListener(e, v[e]); });
        else if (k === 'style') Object.keys(v).forEach(function (p) { node.style.setProperty(p, v[p]); });
        else node.setAttribute(k, v === true ? '' : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(parent, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(parent, c); }); return; }
    parent.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function isDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function lineColor(line) { return LINE_COLORS[line] || LINE_FALLBACK; }

  function gradeColor(grade) {
    var pair = GRADE_COLORS[grade] || NEUTRAL;
    return isDark() ? pair.dark : pair.light;
  }

  /** 백엔드와 동일한 임계값: <80 / <130 / <150 / >=150 */
  function gradeOf(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) return null;
    if (pct < 80) return '여유';
    if (pct < 130) return '보통';
    if (pct < 150) return '혼잡';
    return '매우혼잡';
  }

  function hexToRgba(hex, alpha) {
    var v = hex.replace('#', '');
    var n = parseInt(v, 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha + ')';
  }

  function pctText(v) {
    return (v === null || v === undefined) ? '—' : String(Math.round(v));
  }

  function secToMin(sec) {
    if (sec === null || sec === undefined) return null;
    return Math.max(0, Math.round(sec / 60));
  }

  function timeText(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return String(n).padStart(2, '0'); };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /** 노선 뱃지에 넣을 짧은 라벨. 숫자 노선은 숫자, 나머지는 첫 글자. */
  function lineGlyph(line) {
    var m = /^(\d)호선$/.exec(line);
    if (m) return m[1];
    if (line === 'GTXA') return 'A';
    if (/^인천(\d)호선$/.test(line)) return '인' + /^인천(\d)호선$/.exec(line)[1];
    return line.charAt(0);
  }

  /* ---------------------------------------------------------
     2. API
     --------------------------------------------------------- */

  function api(path, params) {
    var qs = '';
    if (params) {
      var sp = new URLSearchParams();
      Object.keys(params).forEach(function (k) {
        if (params[k] !== '' && params[k] !== null && params[k] !== undefined) sp.set(k, params[k]);
      });
      var s = sp.toString();
      if (s) qs = '?' + s;
    }
    return fetch(path + qs, { headers: { Accept: 'application/json' } }).then(function (res) {
      return res.text().then(function (body) {
        var data = null;
        try { data = JSON.parse(body); } catch (e) { /* 비 JSON 응답 */ }
        if (!res.ok) {
          var err = new Error((data && data.detail) || ('HTTP ' + res.status));
          err.status = res.status;
          err.detail = data && data.detail;
          throw err;
        }
        if (data === null) throw new Error('응답을 해석할 수 없습니다.');
        return data;
      });
    });
  }

  /* ---------------------------------------------------------
     3. 상태
     --------------------------------------------------------- */

  var S = {
    health: null,
    lines: [],
    line: DEFAULT_LINE,
    lineStations: [],
    stationName: null,
    dest: '',
    heatDirection: '',
    heat: null,
    running: true,
    countdown: REFRESH_SEC,
    seq: { positions: 0, predict: 0, heat: 0 }
  };

  var dom = {};
  var map = null, canvasRenderer = null;
  var layers = { network: null, line: null, stations: null, trains: null };
  var stationMarkers = {};

  /* ---------------------------------------------------------
     4. 오류/빈 상태 헬퍼
     --------------------------------------------------------- */

  function errorBox(message, onRetry) {
    return h('div', { class: 'inline-error', role: 'alert' },
      h('b', { text: '불러오지 못했습니다' }),
      h('span', { text: message }),
      h('button', {
        type: 'button', class: 'btn btn--ghost btn--sm', text: '다시 시도',
        on: { click: onRetry }
      })
    );
  }

  function emptyBox(icon, title, desc) {
    return h('div', { class: 'empty' },
      h('span', { class: 'empty__icon', 'aria-hidden': 'true', text: icon }),
      h('strong', { text: title }),
      h('span', { text: desc })
    );
  }

  /* ---------------------------------------------------------
     5. 고지 배너 (정직성 요구사항)
     --------------------------------------------------------- */

  function renderNotices() {
    var box = dom.notices;
    clear(box);
    var health = S.health;
    if (!health) return;

    if (health.realtimeEnabled === false) {
      box.appendChild(h('div', { class: 'notice', role: 'note' },
        h('span', { class: 'notice__icon', 'aria-hidden': 'true', text: '!' }),
        h('div', {},
          h('b', { text: '실시간 지하철 인증키가 설정되지 않았습니다' }),
          h('span', { text: '실시간 열차 위치·도착 정보를 받아올 수 없습니다. 아래에서 별도의 ' }),
          h('code', { text: '실시간 지하철 인증키' }),
          h('span', { text: '를 신청해 등록하기 전까지, 화면의 열차 정보는 비어 있거나 녹화된 재생 데이터입니다. ' }),
          h('a', {
            href: KEY_URL, target: '_blank', rel: 'noopener noreferrer',
            text: '서울열린데이터광장에서 실시간 인증키 신청 ↗'
          })
        )
      ));
    }

    if (health.congestionSource && health.congestionSource !== 'official') {
      box.appendChild(h('div', { class: 'notice', role: 'note' },
        h('span', { class: 'notice__icon', 'aria-hidden': 'true', text: '≈' }),
        h('div', {},
          h('b', { text: '이 화면의 혼잡도는 모두 추정치입니다' }),
          h('span', {
            text: '공식 측정 혼잡도가 아니라 승하차 인원 통계로부터 계산한 추정값입니다(' +
                  'congestionSource: ' + health.congestionSource + '). ' +
                  '실제 체감과 다를 수 있으며, 값은 언제나 정원 대비 백분율(%)이지 탑승 인원수가 아닙니다.'
          })
        )
      ));
    }

    if (health.dataReady === false) {
      box.appendChild(h('div', { class: 'notice', role: 'alert' },
        h('span', { class: 'notice__icon', 'aria-hidden': 'true', text: '×' }),
        h('div', {},
          h('b', { text: '역 데이터가 적재되지 않았습니다' }),
          h('span', { text: 'ETL을 먼저 실행해야 노선도와 예측이 동작합니다.' })
        )
      ));
    }

    dom.estimateTag.hidden = !(health.congestionSource && health.congestionSource !== 'official');
  }

  /** 재생/실시간 배지. replay 는 절대 감추지 않는다. */
  function setSourceTag(source) {
    var tag = dom.sourceTag;
    if (!source) { tag.hidden = true; return; }
    tag.hidden = false;
    tag.dataset.source = source;
    if (source === 'replay') {
      tag.textContent = '재생 모드 (녹화 데이터)';
      tag.title = '실시간 API 대신 이전에 녹화해 둔 응답을 재생하고 있습니다. 지금 이 순간의 열차 위치가 아닙니다.';
    } else if (source === 'live') {
      tag.textContent = '실시간';
      tag.title = '실시간 API 응답입니다.';
    } else {
      tag.textContent = String(source);
      tag.title = '';
    }
  }

  /* ---------------------------------------------------------
     6. 노선 선택 바
     --------------------------------------------------------- */

  function renderLineChips() {
    var box = dom.lineChips;
    clear(box);
    S.lines.forEach(function (info) {
      var color = lineColor(info.line);
      var chip = h('button', {
        type: 'button', role: 'tab', class: 'line-chip',
        'aria-selected': String(info.line === S.line),
        style: { '--c': color },
        title: info.line + ' · ' + info.stationCount + '개 역' +
               (info.predictionAvailable ? ' · 혼잡 예측 가능' : ' · 도착 정보만 제공'),
        on: { click: function () { selectLine(info.line); } }
      },
        h('span', { class: 'line-chip__bullet', 'aria-hidden': 'true', text: lineGlyph(info.line) }),
        h('span', { text: info.line }),
        h('span', {
          class: 'line-chip__flag',
          text: info.predictionAvailable ? '예측' : '도착만'
        })
      );
      box.appendChild(chip);
    });
  }

  function applyLineTheme(line) {
    document.documentElement.style.setProperty('--line', lineColor(line));
  }

  /* ---------------------------------------------------------
     7. 지도
     --------------------------------------------------------- */

  /**
   * 배경 지도 타일.
   *
   * 라이브러리(Leaflet)는 vendor/ 에 내려받아 로컬에서 서빙하지만, 타일은 외부
   * (OpenStreetMap)에서 받는다. 앱이 어차피 서울시 실시간 API 를 호출하므로
   * 네트워크는 이미 전제되어 있고, 배경이 있어야 열차 위치가 어디인지 읽힌다.
   *
   * 다만 발표장 네트워크가 막히는 경우가 있어, 타일이 계속 실패하면 레이어를 걷어내
   * 배경 없이 노선·마커만으로 계속 동작한다. 지도가 죽어도 예측 화면은 살아 있어야 한다.
   */
  function addBasemap() {
    var failures = 0;
    var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '지도 © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      crossOrigin: true
    });

    tiles.on('tileerror', function () {
      failures += 1;
      // 타일 한두 장 실패는 흔하다. 반복되면 그때 배경을 접는다.
      if (failures < 6 || !map.hasLayer(tiles)) return;
      map.removeLayer(tiles);   // 컨테이너의 격자 배경이 그대로 드러난다
      map.attributionControl.addAttribution('타일 불러오기 실패 · 배경 없이 표시');
    });

    tiles.addTo(map);
  }

  function initMap() {
    map = L.map('map', {
      zoomControl: true,
      attributionControl: true,
      minZoom: 8,
      maxZoom: 17,
      zoomSnap: 0.25,
      wheelPxPerZoomLevel: 120
    }).setView([37.5563, 126.9723], 11);

    map.attributionControl.setPrefix('');
    // 좁은 화면에서 줄바꿈되며 범례를 덮지 않도록 짧게. 전체 출처는 푸터에 있다.
    map.attributionControl.addAttribution('실좌표 · 서울열린데이터광장');
    map.zoomControl.setPosition('topright');

    addBasemap();

    canvasRenderer = L.canvas({ padding: 0.4 });
    layers.network = L.layerGroup().addTo(map);
    layers.line = L.layerGroup().addTo(map);
    layers.stations = L.layerGroup().addTo(map);
    layers.trains = L.layerGroup().addTo(map);
  }

  /** 전 노선을 옅게 깔아 서울 지하철망의 형태를 배경으로 만든다. */
  function drawNetwork(stations) {
    layers.network.clearLayers();
    groupPolylines(stations).forEach(function (g) {
      if (g.points.length < 2) return;
      L.polyline(g.points, {
        color: lineColor(g.line),
        weight: 1.5,
        opacity: isDark() ? 0.34 : 0.28,
        interactive: false,
        renderer: canvasRenderer,
        smoothFactor: 1.6
      }).addTo(layers.network);
    });
  }

  /**
   * 폴리라인 그룹을 만든다.
   *
   * branchNo 는 0 이면 본선, 그 외 값은 그 지선이 갈라져 나온 지점 번호다
   * (2호선 성수지선=211, 신정지선=234). 즉 한 지선의 역들은 같은 값을 갖는다.
   *
   * 그래도 seq 가 연속인 구간으로 끊는 방식을 쓴다. 지선 번호에 의존하지 않으므로
   * 원천 데이터가 바뀌어도 형태가 무너지지 않고, 무엇보다 분기점(바로 앞 본선 역,
   * seq-1)을 앞에 붙여야 지선이 본선에서 뻗어 나온 모양으로 그려지기 때문이다.
   */
  function groupPolylines(stations) {
    var byLine = {};
    stations.forEach(function (st) {
      if (st.lat === null || st.lng === null) return;
      (byLine[st.line] = byLine[st.line] || []).push(st);
    });

    var pt = function (st) { return [st.lat, st.lng]; };
    var out = [];

    Object.keys(byLine).forEach(function (line) {
      var rows = byLine[line].sort(function (a, b) { return a.seq - b.seq; });
      var bySeq = {};
      rows.forEach(function (r) { bySeq[r.seq] = r; });

      // 본선: branchNo === 0 만. seq 가 중간에 건너뛰어도(지선 번호가 끼어도) 본선끼리는 인접이다.
      var main = rows.filter(function (r) { return r.branchNo === 0; });
      if (main.length > 1) {
        var mainPts = main.map(pt);
        if (line === '2호선' && mainPts.length > 2) mainPts.push(mainPts[0]); // 순환선
        out.push({ line: line, branch: 0, points: mainPts });
      }

      // 지선: seq 가 연속인 branchNo>0 구간마다 하나씩, 분기점(seq-1 본선 역)을 앞에 붙인다.
      var run = [];
      var flush = function () {
        if (!run.length) return;
        var anchor = bySeq[run[0].seq - 1];
        var pts = (anchor && anchor.branchNo === 0 ? [anchor] : []).concat(run).map(pt);
        if (pts.length > 1) out.push({ line: line, branch: 1, points: pts });
        run = [];
      };
      rows.forEach(function (r) {
        if (r.branchNo > 0) {
          if (run.length && r.seq !== run[run.length - 1].seq + 1) flush();
          run.push(r);
        } else {
          flush();
        }
      });
      flush();
    });

    return out;
  }

  function drawSelectedLine(stations) {
    layers.line.clearLayers();
    layers.stations.clearLayers();
    stationMarkers = {};

    var color = lineColor(S.line);

    groupPolylines(stations).forEach(function (g) {
      if (g.points.length < 2) return;
      L.polyline(g.points, {
        color: color,
        weight: g.branch === 0 ? 5 : 3,
        opacity: g.branch === 0 ? 0.95 : 0.7,
        lineCap: 'round', lineJoin: 'round',
        interactive: false
      }).addTo(layers.line);
    });

    var pts = [];
    stations.forEach(function (st) {
      if (st.lat === null || st.lng === null) return;
      pts.push([st.lat, st.lng]);
      var marker = L.circleMarker([st.lat, st.lng], {
        radius: st.transfer ? 6 : 4.5,
        color: color,
        weight: 3,
        fillColor: isDark() ? '#141A22' : '#FFFFFF',
        fillOpacity: 1,
        className: 'station-marker'
      });
      marker._baseRadius = st.transfer ? 6 : 4.5;
      marker.bindTooltip(
        st.name + (st.transfer ? ' (환승)' : '') + (st.branchNo ? ' · 지선' : ''),
        { direction: 'top', offset: [0, -6] }
      );
      marker.on('click', function () { selectStation(st.name); });
      marker.addTo(layers.stations);
      stationMarkers[st.name] = marker;
    });

    if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [34, 34], maxZoom: 13.5 });
  }

  function highlightStation(name) {
    Object.keys(stationMarkers).forEach(function (key) {
      var m = stationMarkers[key];
      var on = key === name;
      m.setStyle({
        radius: on ? 8.5 : m._baseRadius,
        weight: on ? 4 : 3,
        fillColor: on ? lineColor(S.line) : (isDark() ? '#141A22' : '#FFFFFF')
      });
      if (on) {
        var el = m.getElement();
        if (el) { el.classList.remove('station-marker--active'); void el.offsetWidth; el.classList.add('station-marker--active'); }
      }
    });
  }

  /* --- 열차 마커 --------------------------------------------------------- */

  /** 히트맵의 "현재 시간대" 열 인덱스. 운행 시간대 밖이면 -1. */
  function currentSlotIndex() {
    if (!S.heat || !S.heat.slots) return -1;
    var hh = String(new Date().getHours()).padStart(2, '0') + ':00';
    return S.heat.slots.indexOf(hh);
  }

  /**
   * 열차가 서 있는 역의 시간대 혼잡도. **열차별 예측이 아니라 배경 통계값**이다.
   * 열차별 예측은 역을 클릭했을 때 /api/predict 가 준다.
   *
   * 값의 출처(공식 통계 / 승하차 추정)는 역마다 다르다. 공식 통계는 서울교통공사
   * 구간만 다루므로 같은 노선이라도 추정치뿐인 역이 있다. 그래서 값과 출처를 함께
   * 돌려주고, 화면에서 어느 쪽인지 반드시 밝힌다.
   */
  function stationEstimate(train) {
    var idx = currentSlotIndex();
    if (idx < 0 || !S.heat) return null;
    var row = null;
    for (var i = 0; i < S.heat.stations.length; i++) {
      var r = S.heat.stations[i];
      if (r.name === train.stationName || (train.seq !== null && r.seq === train.seq)) { row = r; break; }
    }
    if (!row) return null;
    var v = row.values[idx];
    if (v === null || v === undefined) return null;
    return { pct: v, source: row.source || S.heat.source };
  }

  function drawTrains(payload) {
    layers.trains.clearLayers();
    var trains = payload.trains || [];
    var plotted = 0;

    trains.forEach(function (t) {
      if (t.lat === null || t.lng === null) return;
      plotted++;
      var est = stationEstimate(t);
      var grade = est ? gradeOf(est.pct) : null;
      var fill = grade ? gradeColor(grade) : (isDark() ? NEUTRAL.dark : NEUTRAL.light);

      var marker = L.circleMarker([t.lat, t.lng], {
        radius: 7.5,
        color: isDark() ? '#0B0F14' : '#FFFFFF',
        weight: 2.5,
        fillColor: fill,
        fillOpacity: 1,
        className: 'train-marker'
      });

      var slotLabel = S.heat && currentSlotIndex() >= 0
        ? S.heat.slots[currentSlotIndex()] : '';
      var statLine;
      if (grade) {
        // 값이 공식 통계인지 추정치인지 반드시 밝힌다. 라벨을 고정해 두면
        // 공식 데이터가 들어와도 계속 '추정'이라 말하거나 그 반대가 된다.
        statLine = (est.source === 'official' ? '공식 혼잡도' : '추정 혼잡도')
          + ' ' + Math.round(est.pct) + '% · ' + grade;
      } else {
        statLine = '이 시간대 혼잡도 자료 없음';
      }

      var lines = [
        (t.trainNo || '열차') + '  ' + (t.direction || '') + (t.express ? '  급행' : ''),
        (t.stationName || '?') + ' ' + (t.positionStatus || ''),
        (t.terminalStation ? t.terminalStation + '행' : ''),
        // 이 값은 '이 열차'가 아니라 '이 역·시간대'의 통계다. 혼동하면 안 된다.
        (t.stationName || '이 역') + (slotLabel ? ' ' + slotLabel : '') + ' 기준',
        statLine
      ].filter(Boolean);

      marker.bindTooltip(lines.join('\n'), { direction: 'top', offset: [0, -8], className: 'train-tip' });
      marker.on('click', function () { if (t.stationName) selectStation(t.stationName); });
      marker.addTo(layers.trains);
    });

    var skipped = trains.length - plotted;
    dom.mapCount.textContent =
      plotted + '대 표시' + (skipped > 0 ? ' · 좌표 미매칭 ' + skipped + '대 제외' : '') +
      (payload.fetchedAt ? ' · ' + timeText(payload.fetchedAt) : '');

    dom.mapEmpty.hidden = plotted > 0;
    if (plotted === 0) {
      dom.mapEmptyReason.textContent = S.health && S.health.realtimeEnabled === false
        ? '실시간 인증키가 없어 열차 위치를 받아올 수 없습니다. 노선·역·히트맵은 정상 동작합니다.'
        : (trains.length > 0
            ? '수신된 열차의 역명을 좌표에 매칭하지 못했습니다.'
            : '지금 이 노선에서 수신된 열차가 없습니다.');
    }
  }

  /* ---------------------------------------------------------
     8. 데이터 로딩
     --------------------------------------------------------- */

  function loadHealth() {
    return api('/api/health').then(function (data) {
      S.health = data;
      renderNotices();
    }).catch(function (err) {
      clear(dom.notices);
      dom.notices.appendChild(errorBox('상태 확인 실패: ' + err.message, loadHealth));
    });
  }

  function loadLines() {
    return api('/api/lines').then(function (data) {
      S.lines = data.lines || [];
      renderLineChips();
      var exists = S.lines.some(function (l) { return l.line === S.line; });
      selectLine(exists ? S.line : (S.lines[0] && S.lines[0].line));
    }).catch(function (err) {
      clear(dom.lineChips);
      dom.lineChips.appendChild(errorBox('노선 목록 실패: ' + err.message, loadLines));
    });
  }

  function loadNetwork() {
    return api('/api/stations').then(function (data) {
      drawNetwork(data.stations || []);
    }).catch(function () { /* 배경 레이어는 실패해도 화면을 막지 않는다 */ });
  }

  function selectLine(line) {
    if (!line) return;
    S.line = line;
    S.stationName = null;
    S.dest = '';
    S.heat = null;
    applyLineTheme(line);
    renderLineChips();

    layers.trains.clearLayers();
    dom.mapCount.textContent = '불러오는 중…';
    resetStationPanel();
    resetTimelinePanel();

    loadStations().then(function () {
      // 히트맵을 먼저 받아야 열차 마커에 시간대 추정 등급을 입힐 수 있다.
      return loadHeatmap();
    }).then(function () {
      return loadPositions();
    });
  }

  function loadStations() {
    dom.mapError.hidden = true;
    return api('/api/stations', { line: S.line }).then(function (data) {
      S.lineStations = data.stations || [];
      drawSelectedLine(S.lineStations);
      renderStationSelect();
      renderDestSelect();
    }).catch(function (err) {
      dom.mapError.hidden = false;
      clear(dom.mapError);
      dom.mapError.appendChild(errorBox('역 목록 실패: ' + err.message, loadStations));
    });
  }

  function loadPositions() {
    var token = ++S.seq.positions;
    return api('/api/realtime/positions', { line: S.line }).then(function (data) {
      if (token !== S.seq.positions) return;
      setSourceTag(data.source);
      dom.fetchedTag.hidden = false;
      dom.fetchedTag.textContent = '수신 ' + (timeText(data.fetchedAt) || '—');
      drawTrains(data);
    }).catch(function (err) {
      if (token !== S.seq.positions) return;
      dom.mapCount.textContent = '열차 정보 실패';
      dom.mapError.hidden = false;
      clear(dom.mapError);
      dom.mapError.appendChild(errorBox('실시간 위치 실패: ' + err.message, loadPositions));
    });
  }

  /* ---------------------------------------------------------
     9. 역 선택 / 이번 vs 다음
     --------------------------------------------------------- */

  function renderStationSelect() {
    var sel = dom.stationSelect;
    clear(sel);
    sel.appendChild(h('option', { value: '', text: '역을 선택하세요 (' + S.lineStations.length + ')' }));
    S.lineStations.slice().sort(function (a, b) { return a.seq - b.seq; }).forEach(function (st) {
      sel.appendChild(h('option', {
        value: st.name,
        selected: st.name === S.stationName
      }, st.name + (st.branchNo ? ' · 지선' : '')));
    });
    sel.value = S.stationName || '';
  }

  function renderDestSelect() {
    var sel = dom.destSelect;
    clear(sel);
    sel.appendChild(h('option', { value: '', text: '목적지 선택 (선택 사항)' }));
    S.lineStations
      .filter(function (st) { return st.branchNo === 0; })
      .sort(function (a, b) { return a.seq - b.seq; })
      .forEach(function (st) {
        if (st.name === S.stationName) return;
        sel.appendChild(h('option', { value: st.name }, st.name));
      });
    sel.value = S.dest || '';
  }

  function resetStationPanel() {
    clear(dom.stationBody);
    dom.stationBody.appendChild(emptyBox('◎', '지도에서 역을 선택하세요',
      '선택한 역으로 오는 이번 열차와 다음 열차의 예상 혼잡도를 비교합니다.'));
  }

  function resetTimelinePanel() {
    clear(dom.timelineBody);
    dom.timelineBody.appendChild(emptyBox('◷', '목적지를 고르면 착석 예상 구간을 계산합니다',
      '역을 선택하고 목적지를 지정하면, 어느 역쯤부터 앉을 확률이 올라가는지 보여줍니다.'));
  }

  function selectStation(name) {
    if (!name) return;
    S.stationName = name;
    dom.stationSelect.value = name;
    highlightStation(name);
    renderDestSelect();
    loadPrediction();
    if (window.innerWidth < 1100) {
      dom.stationPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function loadPrediction() {
    if (!S.stationName) return Promise.resolve();
    var token = ++S.seq.predict;

    // direction 은 의도적으로 보내지 않는다. 실시간 응답의 방향 표기(상행/하행)와
    // 예측 API 의 필터 값(상선/하선)이 서로 달라 후보가 통째로 걸러질 수 있다.
    return api('/api/predict/station/' + encodeURIComponent(S.stationName), {
      line: S.line,
      dest: S.dest || undefined
    }).then(function (data) {
      if (token !== S.seq.predict) return;
      setSourceTag(data.source);
      renderPrediction(data);
      renderTimeline(data);
    }).catch(function (err) {
      if (token !== S.seq.predict) return;
      clear(dom.stationBody);
      dom.stationBody.appendChild(errorBox(
        err.status === 404 ? (err.detail || '이 노선에 해당 역이 없습니다.') : err.message,
        loadPrediction
      ));
    });
  }

  function stationMeta(name) {
    for (var i = 0; i < S.lineStations.length; i++) {
      if (S.lineStations[i].name === name) return S.lineStations[i];
    }
    return null;
  }

  function renderPrediction(data) {
    var body = dom.stationBody;
    clear(body);

    var meta = stationMeta(data.station);
    body.appendChild(h('div', { class: 'station-head' },
      h('span', { class: 'station-head__line', text: data.line }),
      h('h3', { text: data.station }),
      meta && meta.transfer ? h('span', { class: 'badge badge--transfer', text: '환승역' }) : null,
      h('span', { class: 'station-head__meta num', text: timeText(data.fetchedAt) })
    ));

    if (data.source === 'replay') {
      body.appendChild(h('p', { class: 'chip', style: { 'margin-bottom': '12px' } },
        '재생 모드 · 녹화된 응답 기준'));
    }

    // (a) 예측 불가 노선: 도착 정보만 정직하게
    if (data.predictionAvailable === false) {
      body.appendChild(h('div', { class: 'empty', style: { 'margin-bottom': '12px' } },
        h('span', { class: 'empty__icon', 'aria-hidden': 'true', text: '⊘' }),
        h('strong', { text: '이 노선은 혼잡도를 예측하지 않습니다' }),
        h('span', { text: data.reason || '공개된 혼잡도 통계가 없는 노선입니다.' })
      ));
      var arrivals = data.arrivals || [];
      if (!arrivals.length) {
        body.appendChild(emptyBox('⋯', '표시할 도착 정보가 없습니다',
          '실시간 인증키가 등록되면 도착 예정 열차가 여기에 표시됩니다.'));
        return;
      }
      body.appendChild(h('ul', { class: 'arrivals' }, arrivals.map(function (a) {
        var min = secToMin(a.etaSec);
        return h('li', {},
          h('span', { class: 'arrivals__eta', text: min === null ? '—' : min + '분' }),
          h('span', { class: 'arrivals__dest', text: (a.terminalStation ? a.terminalStation + '행' : '행선지 미상') }),
          a.express ? h('span', { class: 'badge badge--express', text: '급행' }) : null,
          a.direction ? h('span', { class: 'badge', text: a.direction }) : null,
          h('span', { class: 'arrivals__no', text: a.trainNo || '' })
        );
      })));
      return;
    }

    // (b) 열차 없음
    if (!data.thisTrain) {
      body.appendChild(emptyBox('◌', '지금 도착 예정인 열차가 없습니다',
        data.reason || '실시간 열차 정보가 비어 있습니다.'));
      return;
    }

    // (c) 추천 배너
    var rec = data.recommendation;
    if (rec) {
      body.appendChild(h('div', { class: 'verdict', 'data-verdict': rec.verdict, role: 'status' },
        h('span', { class: 'verdict__key', text: VERDICT_LABEL[rec.verdict] || rec.verdict }),
        h('p', { class: 'verdict__msg' },
          rec.message,
          rec.differencePct !== null && rec.differencePct !== undefined
            ? h('span', { class: 'verdict__delta', text: '혼잡도 차이 ' + Math.abs(Math.round(rec.differencePct)) + '%p' })
            : null
        )
      ));
    }

    body.appendChild(h('div', { class: 'train-grid' },
      trainCard(data.thisTrain, '이번 열차', true),
      data.nextTrain
        ? trainCard(data.nextTrain, '다음 열차', false)
        : h('div', { class: 'train-card' },
            h('div', { class: 'train-card__top' }, h('span', { class: 'train-card__rank', text: 'NEXT · 다음 열차' })),
            h('div', { class: 'train-card__body' },
              h('p', { class: 'empty', style: { padding: '18px 10px' } },
                h('strong', { text: '다음 열차 정보 없음' }),
                h('span', { text: '비교할 두 번째 도착 열차가 아직 수신되지 않았습니다.' })))
          )
    ));

    // 게이지 애니메이션은 다음 프레임에 폭을 넣어 트리거한다.
    requestAnimationFrame(function () {
      Array.prototype.forEach.call(body.querySelectorAll('.gauge__fill'), function (el) {
        el.style.width = el.dataset.width;
      });
    });
  }

  function trainCard(train, label, primary) {
    var grade = train.grade;
    var min = train.etaMin;
    var width = Math.max(2, Math.min(100, (train.expectedPct / GAUGE_MAX) * 100));
    var delta = (train.expectedPct !== null && train.baselinePct !== null)
      ? train.expectedPct - train.baselinePct : null;

    return h('article', { class: 'train-card' + (primary ? ' train-card--primary' : '') },
      h('header', { class: 'train-card__top' },
        h('span', { class: 'train-card__rank', text: (primary ? 'THIS · ' : 'NEXT · ') + label }),
        h('span', { class: 'train-card__no num', text: train.trainNo ? '#' + train.trainNo : '' })
      ),
      h('div', { class: 'train-card__body' },

        h('div', { class: 'eta' },
          h('span', { class: 'eta__val', text: min === null || min === undefined ? '—' : String(min) }),
          h('span', { class: 'eta__unit', text: '분 후 도착' }),
          train.etaSec !== null && train.etaSec !== undefined
            ? h('span', { class: 'eta__sec', text: train.etaSec + 's' }) : null
        ),

        h('div', { class: 'pct' },
          h('span', { class: 'pct__val', 'data-grade': grade },
            pctText(train.expectedPct), h('small', { text: '%' })),
          h('span', { class: 'grade-badge', 'data-grade': grade, text: grade || '등급 미상' })
        ),

        h('div', {},
          h('div', { class: 'gauge', role: 'img',
                     'aria-label': '예상 혼잡도 ' + pctText(train.expectedPct) + '퍼센트, 등급 ' + (grade || '미상') },
            h('i', { class: 'gauge__fill', 'data-grade': grade, 'data-width': width + '%' }),
            h('i', { class: 'gauge__tick', style: { left: (80 / GAUGE_MAX * 100) + '%' } }),
            h('i', { class: 'gauge__tick', style: { left: (130 / GAUGE_MAX * 100) + '%' } }),
            h('i', { class: 'gauge__tick', style: { left: (150 / GAUGE_MAX * 100) + '%' } })
          ),
          h('div', { class: 'gauge__scale', 'aria-hidden': 'true' },
            h('span', { text: '0' }), h('span', { text: '80' }),
            h('span', { text: '130' }), h('span', { text: '150' }), h('span', { text: '200%' }))
        ),

        h('dl', { class: 'kv' },
          h('dt', { text: '평소 이 시간대' }),
          h('dd', { class: 'num', text: pctText(train.baselinePct) + '%' }),
          h('dt', { text: '보정' }),
          h('dd', { class: 'num', text: delta === null ? '—' : (delta >= 0 ? '+' : '') + Math.round(delta) + '%p' }),
          h('dt', { text: '행선지' }),
          h('dd', { text: train.terminalStation ? train.terminalStation + '행' : '—' }),
          h('dt', { text: '운행' }),
          h('dd', { text: train.express ? '급행' : '일반' }),
          train.headway && train.headway.available && train.headway.sec !== null
            ? h('dt', { text: '앞 열차 간격' }) : null,
          train.headway && train.headway.available && train.headway.sec !== null
            ? h('dd', { class: 'num', text: (train.headway.sec / 60).toFixed(1) + '분' +
                (train.headway.nominalSec ? ' (평소 ' + (train.headway.nominalSec / 60).toFixed(1) + '분)' : '') })
            : null
        ),

        h('div', { style: { display: 'flex', gap: '6px', 'flex-wrap': 'wrap' } },
          train.express ? h('span', { class: 'badge badge--express', text: '급행' }) : null,
          train.baselineSource === 'estimated'
            ? h('span', { class: 'badge badge--estimate', text: '추정치 (승하차 통계 기반)' }) : null,
          train.baselineResolution
            ? h('span', { class: 'badge', text: '기준 해상도 ' + train.baselineResolution }) : null
        ),

        (train.reasons && train.reasons.length)
          ? h('div', { class: 'reasons' },
              h('p', { class: 'reasons__title', text: 'WHY · 이렇게 예측한 이유' }),
              h('ul', {}, train.reasons.map(function (r) { return h('li', { text: r }); })))
          : null
      )
    );
  }

  /* ---------------------------------------------------------
     10. 착석 타이밍 타임라인
     --------------------------------------------------------- */

  function renderTimeline(data) {
    var body = dom.timelineBody;
    clear(body);

    if (!S.dest) {
      body.appendChild(emptyBox('◷', '목적지를 고르면 착석 예상 구간을 계산합니다',
        S.stationName
          ? '"' + S.stationName + '"에서 출발한다고 보고, 어느 역쯤부터 앉을 확률이 올라가는지 계산합니다.'
          : '역을 먼저 선택한 뒤 목적지를 지정하세요.'));
      return;
    }

    if (data.predictionAvailable === false) {
      body.appendChild(emptyBox('⊘', '이 노선은 착석 예측을 제공하지 않습니다',
        data.reason || '혼잡도 통계가 없는 노선입니다.'));
      return;
    }

    var tl = data.timeline;
    if (!tl) {
      body.appendChild(emptyBox('◌', '착석 타임라인을 계산할 수 없습니다',
        data.reason || '도착 예정 열차가 있어야 승객 감소 곡선을 추정할 수 있습니다. 실시간 열차 정보가 비어 있습니다.'));
      return;
    }

    var stops = tl.stops || [];
    if (!stops.length) {
      body.appendChild(emptyBox('◌', '경로를 찾지 못했습니다',
        tl.reason || '본선에서 ' + S.stationName + ' → ' + tl.destination + ' 경로를 찾지 못했습니다.'));
      return;
    }

    var seatIdx = (tl.seatFromIndex === null || tl.seatFromIndex === undefined) ? -1 : tl.seatFromIndex;

    body.appendChild(h('div', { class: 'timeline-summary' },
      h('span', { class: 'timeline-summary__icon', 'aria-hidden': 'true', text: seatIdx >= 0 ? '↓' : '⋯' }),
      h('div', {},
        seatIdx >= 0 && tl.seatFrom
          ? h('b', { text: tl.seatFrom + '역부터 앉을 확률이 올라갑니다' })
          : h('b', { text: tl.destination + '까지 앉기 어려울 것으로 보입니다' }),
        h('span', {
          text: (seatIdx >= 0 && tl.seatAfterMinutes !== null && tl.seatAfterMinutes !== undefined
                  ? '탑승 후 약 ' + tl.seatAfterMinutes + '분 · '
                  : '') +
                '목적지 ' + tl.destination + ' · 정차 ' + stops.length + '개 역 · 출처 ' +
                (tl.source === 'estimated' ? '추정치(승하차 통계)' : tl.source)
        })
      ),
      tl.source === 'estimated' ? h('span', { class: 'badge badge--estimate', text: '추정치' }) : null
    ));

    var list = h('div', { class: 'timeline' });

    stops.forEach(function (stop, i) {
      if (i === seatIdx) {
        list.appendChild(h('div', { class: 'tl-seatmark' },
          '여기서부터 앉을 확률↑',
          h('span', { text: stop.name + ' · 승차 후 ' + stop.minutesFromNow + '분' })
        ));
      }

      var grade = stop.grade || gradeOf(stop.congestionPct);
      var width = stop.congestionPct === null || stop.congestionPct === undefined
        ? 0 : Math.max(2, Math.min(100, (stop.congestionPct / GAUGE_MAX) * 100));

      var cls = 'tl-stop';
      if (i === 0) cls += ' tl-stop--now';
      if (stop.seatLikely) cls += ' tl-stop--seat';

      list.appendChild(h('div', {
        class: cls,
        style: { 'animation-delay': Math.min(i * 28, 500) + 'ms' }
      },
        h('span', { class: 'tl-stop__time', text: (i === 0 ? '지금' : '+' + stop.minutesFromNow + '분') }),
        h('span', { class: 'tl-stop__rail', 'aria-hidden': 'true' }, h('i', { class: 'tl-stop__dot' })),
        h('span', { class: 'tl-stop__name' },
          stop.name,
          h('small', { text: (stop.timeSlot || '') + (stop.seatLikely ? ' · 착석 가능' : '') })),
        h('span', {
          class: 'tl-stop__bar', role: 'img',
          'aria-label': stop.name + ' 예상 혼잡도 ' + pctText(stop.congestionPct) + '퍼센트, ' + (grade || '등급 미상')
        }, h('i', { 'data-grade': grade, 'data-width': width + '%' })),
        h('span', { class: 'tl-stop__pct' },
          pctText(stop.congestionPct) + '%',
          grade ? h('em', { 'data-grade': grade, text: grade }) : null)
      ));
    });

    body.appendChild(list);
    body.appendChild(h('p', {
      class: 'map-legend__foot', style: { 'margin-top': '12px', border: '0' },
      text: '착석 확률은 정차역별 하차 추정치를 누적해 계산한 값입니다. 실제 좌석 확보는 차량·출입문 위치에 따라 달라집니다.'
    }));

    requestAnimationFrame(function () {
      Array.prototype.forEach.call(body.querySelectorAll('.tl-stop__bar i'), function (el) {
        el.style.width = el.dataset.width;
      });
    });
  }

  /* ---------------------------------------------------------
     11. 히트맵
     --------------------------------------------------------- */

  /** 등급 대역 안에서 알파를 올려 연속적인 스케일을 만든다. 색과 숫자를 함께 준다. */
  function heatStyle(pct) {
    if (pct === null || pct === undefined) return null;
    var grade = gradeOf(pct), a;
    if (grade === '여유') a = 0.12 + (Math.max(pct, 0) / 80) * 0.43;
    else if (grade === '보통') a = 0.55 + ((pct - 80) / 50) * 0.23;
    else if (grade === '혼잡') a = 0.78 + ((pct - 130) / 20) * 0.12;
    else a = Math.min(1, 0.90 + ((pct - 150) / 70) * 0.10);
    return { color: hexToRgba(gradeColor(grade), a), grade: grade, strong: a >= 0.62 };
  }

  function loadHeatmap() {
    var token = ++S.seq.heat;
    clear(dom.heatBody);
    dom.heatBody.appendChild(h('div', { class: 'skeleton skeleton--grid', 'aria-hidden': 'true' }));

    return api('/api/heatmap', { line: S.line, direction: S.heatDirection || undefined })
      .then(function (data) {
        if (token !== S.seq.heat) return;
        S.heat = data;
        renderHeatmap(data);
      })
      .catch(function (err) {
        if (token !== S.seq.heat) return;
        S.heat = null;
        clear(dom.heatBody);
        if (err.status === 404) {
          dom.heatBody.appendChild(emptyBox('⊘', '이 노선의 혼잡도 데이터가 없습니다',
            err.detail || (S.line + ' 의 시간대별 혼잡 통계가 적재되어 있지 않습니다.')));
        } else {
          dom.heatBody.appendChild(errorBox('히트맵 실패: ' + err.message, loadHeatmap));
        }
      });
  }

  function renderHeatmap(data) {
    clear(dom.heatBody);
    var slots = data.slots || [];
    var rows = data.stations || [];
    if (!rows.length) {
      dom.heatBody.appendChild(emptyBox('⊘', '표시할 역이 없습니다', '이 조건에 해당하는 혼잡 통계가 없습니다.'));
      return;
    }

    var nowIdx = currentSlotIndex();

    var thead = h('thead', {}, h('tr', {},
      h('th', { scope: 'col' }, '역 / 시간대'),
      slots.map(function (s, i) {
        return h('th', {
          scope: 'col',
          style: i === nowIdx ? { color: 'var(--line)', 'font-weight': '800' } : null,
          title: i === nowIdx ? '현재 시간대' : null
        }, s.slice(0, 2) + (i === nowIdx ? '*' : ''));
      })
    ));

    var tbody = h('tbody', {}, rows.map(function (row) {
      return h('tr', {},
        h('th', { scope: 'row', title: row.name },
          h('b', { text: String(row.seq) }), row.name),
        slots.map(function (slot, i) {
          var v = row.values[i];
          var st = heatStyle(v);
          if (!st) {
            return h('td', {
              class: 'heat-cell heat-cell--null', tabindex: '-1',
              'aria-label': row.name + ' ' + slot + ' 데이터 없음'
            }, '·');
          }
          return h('td', {
            class: 'heat-cell' + (st.strong ? ' heat-cell--strong' : ''),
            style: { background: st.color },
            tabindex: '-1',
            title: row.name + ' ' + slot + ' · ' + Math.round(v) + '% · ' + st.grade,
            'aria-label': row.name + ' ' + slot + ' 혼잡도 ' + Math.round(v) + '퍼센트 ' + st.grade
          }, String(Math.round(v)));
        })
      );
    }));

    dom.heatBody.appendChild(h('div', { class: 'heat-scroll', tabindex: '0', role: 'region',
                                        'aria-label': data.line + ' 시간대별 혼잡 히트맵. 좌우로 스크롤하세요.' },
      h('table', { class: 'heat-table' },
        h('caption', { class: 'visually-hidden', style: { position: 'absolute', left: '-9999px' } },
          data.line + ' ' + data.dayType + ' ' + (data.direction || '양방향 평균') + ' 시간대별 추정 혼잡도(%)'),
        thead, tbody)
    ));

    dom.heatBody.appendChild(h('div', { class: 'heat-foot' },
      h('div', { class: 'heat-scale' },
        h('span', { text: '낮음' }),
        h('span', { class: 'heat-scale__bar', 'aria-hidden': 'true' }),
        h('span', { text: '높음' }),
        h('span', { class: 'heat-scale__labels', 'aria-hidden': 'true' },
          h('span', { text: '여유<80' }), h('span', { text: '보통<130' }),
          h('span', { text: '혼잡<150' }), h('span', { text: '매우혼잡≥150' }))
      ),
      h('p', { class: 'panel__note' },
        rowsSummary(data, rows.length) )
    ));
  }

  function rowsSummary(data, count) {
    return data.line + ' · ' + data.dayType + ' · ' + (data.direction || '양방향 평균') +
           ' · ' + count + '개 역 × ' + (data.slots || []).length + '개 시간대 · 출처 ' +
           (data.source === 'estimated' ? '추정치' : data.source);
  }

  /* ---------------------------------------------------------
     12. 자동 갱신
     --------------------------------------------------------- */

  function updateRing() {
    var ratio = S.countdown / REFRESH_SEC;
    dom.refreshProgress.style.strokeDashoffset = String(RING_LEN * (1 - ratio));
    dom.refreshCount.textContent = String(S.countdown);
  }

  function setRunning(running) {
    S.running = running;
    dom.refreshToggle.setAttribute('aria-pressed', String(running));
    dom.refreshToggle.setAttribute('aria-label', running ? '자동 갱신 일시정지' : '자동 갱신 재개');
    dom.refreshToggle.title = running ? '자동 갱신 일시정지' : '자동 갱신 재개';
    dom.refreshHint.textContent = running ? REFRESH_SEC + '초 주기' : '일시정지됨';
    if (running) { S.countdown = REFRESH_SEC; updateRing(); }
  }

  function refreshNow() {
    S.countdown = REFRESH_SEC;
    updateRing();
    loadPositions();
    if (S.stationName) loadPrediction();
  }

  function startClock() {
    updateRing();
    setInterval(function () {
      if (!S.running) return;
      S.countdown -= 1;
      if (S.countdown <= 0) { S.countdown = REFRESH_SEC; refreshNow(); }
      updateRing();
    }, 1000);
  }

  /* ---------------------------------------------------------
     13. 부팅
     --------------------------------------------------------- */

  function cacheDom() {
    dom.notices = $('#notices');
    dom.sourceTag = $('#sourceTag');
    dom.estimateTag = $('#estimateTag');
    dom.fetchedTag = $('#fetchedTag');
    dom.lineChips = $('#lineChips');
    dom.mapCount = $('#mapCount');
    dom.mapEmpty = $('#mapEmpty');
    dom.mapEmptyReason = $('#mapEmptyReason');
    dom.mapError = $('#mapError');
    dom.stationPanel = $('#stationPanel');
    dom.stationBody = $('#stationBody');
    dom.stationSelect = $('#stationSelect');
    dom.destSelect = $('#destSelect');
    dom.timelineBody = $('#timelineBody');
    dom.heatBody = $('#heatBody');
    dom.dirSelect = $('#dirSelect');
    dom.refreshToggle = $('#refreshToggle');
    dom.refreshProgress = $('#refreshProgress');
    dom.refreshCount = $('#refreshCount');
    dom.refreshHint = $('#refreshHint');
  }

  function bindEvents() {
    dom.refreshToggle.addEventListener('click', function () { setRunning(!S.running); });
    $('#refreshNow').addEventListener('click', refreshNow);

    dom.stationSelect.addEventListener('change', function () {
      if (this.value) selectStation(this.value);
      else { S.stationName = null; highlightStation(null); resetStationPanel(); resetTimelinePanel(); }
    });

    dom.destSelect.addEventListener('change', function () {
      S.dest = this.value;
      if (S.stationName) loadPrediction();
      else resetTimelinePanel();
    });

    dom.dirSelect.addEventListener('change', function () {
      S.heatDirection = this.value;
      loadHeatmap();
    });

    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onTheme = function () {
        if (S.lineStations.length) { drawSelectedLine(S.lineStations); if (S.stationName) highlightStation(S.stationName); }
        loadNetwork();
        if (S.heat) renderHeatmap(S.heat);
        loadPositions();
      };
      if (mq.addEventListener) mq.addEventListener('change', onTheme);
      else if (mq.addListener) mq.addListener(onTheme);
    }
  }

  function boot() {
    cacheDom();
    applyLineTheme(S.line);
    initMap();
    bindEvents();
    setRunning(true);
    startClock();
    loadNetwork();
    loadHealth().then(loadLines);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

})();
