// 전역 변수 선언
let isCheckingStatus = false;     // 상태 확인 중 여부
let isStartingRecordings = false; // 중복 녹화 시작 방지
let isStoppingRecordings = false; // 중복 녹화 중지 방지
let isSubmitting = false;         // 중복 요청 방지
let isListenersAdded = false;     // 중복 리스너 방지
let isConfirmed = false;          // 중복 confirm() 방지
let isPollingSys = false;
let lastRecordingDuration = {};
let lastRecordingFilename = {};
let filesCurrentPath = null;
let filesRoots = [];
let filesAllItems = [];           // 파일 검색 필터용 캐시
let selectedPaths = new Set();    // 모바일 선택 상태(경로 Set)
let selectMode = false;           // 모바일: 선택모드 여부
let movePickerPath = null;		  // 폴더 선택 모달의 현재 경로
let isThumbJobRunning = false;    // 썸네일/메타 갱신 중복 방지
let isRefreshingStatus = false;
let __sys_poll_tm  = null;
let __sys_disk_signature = "";
let __sys_dashboard_expanded = true;

const statusPriority = {};        // channelId -> number (높을수록 우선)

// 동시 작업 개수(기기 성능 기반 자동 조절)
let MAX_CONCURRENCY = (() => {
  // 0) URL 파라미터 오버라이드 (예: ?mc=10)
  const urlMc = parseInt(new URLSearchParams(location.search).get('mc'), 10);
  if (Number.isInteger(urlMc) && urlMc > 0) return Math.min(12, urlMc);

  // 1) 서버 설정 오버라이드 (템플릿에서 window.appConfig 주입 시)
  const cfgMc = (window.appConfig && Number.isInteger(window.appConfig.metaConcurrency))
                  ? window.appConfig.metaConcurrency : null;
  if (cfgMc && cfgMc > 0) return Math.min(12, cfgMc);

  // 2) AUTO
  const cores = (navigator.hardwareConcurrency || 2);
  const auto  = Math.floor(cores * 0.75);
  return Math.min(12, Math.max(2, auto));  // 2~12
})();
console.debug("[DEBUG] MAX_CONCURRENCY =", MAX_CONCURRENCY);

// 길게 누르기 판정 시간
const LONG_PRESS_MS = 380;         

// 채널별 상태 폴링 타이머
const pollTimers = {};

// 파일관리에서 숨김으로 간주할 후보 이름(Windows/일반 공통)
const HIDDEN_NAME_SET = new Set([
  'system volume information',
  '$recycle.bin',
  'recycler',
  'pagefile.sys',
  'hiberfil.sys',
  'swapfile.sys',
  'config.msi',
  'msocache',
  'recovery',
  'desktop.ini',
  'thumbs.db'
]);


// 채널별 썸네일/메타 동시성 제어 설정
const FETCH_TIMEOUT_MS = 8000;  // 채널별 요청 타임아웃(ms)
const META_COOLDOWN_MS = 45000;
const lastMetaFetchAt = Object.create(null);


// CSS.escape 폴백(특수문자 있는 채널ID 대응)
const cssEsc = (window.CSS && CSS.escape)
  ? CSS.escape
  : s => String(s).replace(/[^a-zA-Z0-9_\u00A0-\uFFFF-]/g, ch => `\\${ch.codePointAt(0).toString(16)} `);


// 안전하게 키 고르는 헬퍼
const pick = (obj, keys, dflt) => {
  for (const k of keys) if (obj && obj[k] != null && obj[k] !== '') return obj[k];
  return dflt;
};


// 동시성 제한 실행
async function mapLimit(arr, limit, iter) {
  const out = new Array(arr.length);
  let i = 0;
  async function worker() {
    while (true) {
      const idx = i++; if (idx >= arr.length) break;
      try { out[idx] = await iter(arr[idx], idx); } catch (e) { console.error(e); out[idx] = null; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, arr.length) }, worker));
  return out;
}


// 공통 fetch timeout 래퍼
async function fetchWithTimeout(url, opts = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    return await fetch(url, {
      credentials: 'same-origin',
      ...(opts || {}),
      signal: controller.signal
    });
  } finally {
    clearTimeout(timer);
  }
}


// 숨김 판단 헬퍼(리눅스 dot 숨김 + 세트)
function isHiddenItem(nameOrItem){
  const n = (typeof nameOrItem === 'string' ? nameOrItem : (nameOrItem?.name || '')).trim();
  if (!n) return false;
  if (n.startsWith('.')) return true;                  // dot hidden
  return HIDDEN_NAME_SET.has(n.toLowerCase());         // set 매칭
}

// Debounce: 지정 시간 이후에 실행
function debounce(func, wait) {
  let timeout;
  return function (...args) {
    const context = this;
    clearTimeout(timeout);
    timeout = setTimeout(() => func.apply(context, args), wait);
  };
}


// escape 함수
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}


// 부트스트랩
document.addEventListener("DOMContentLoaded", async function () {
  // 중복 등록 방지
  if (isListenersAdded) return;
  isListenersAdded = true;

  // 슬라이드 메뉴 바인딩
  const sidenav = document.getElementById('mySidenav');
  const menuIcon = document.querySelector('.menu-icon');
  const closeBtn = document.querySelector('#mySidenav .closebtn');

  function openNav(e) {
    if (e) e.preventDefault();
    if (!sidenav) return;
    const cs = getComputedStyle(sidenav);
    // 일부 사용자 환경에서 display:none이면 width만 바꿔도 안 보이는 문제 보정
    if (cs.display === 'none') sidenav.style.display = 'block';
    sidenav.style.width = '250px';
    if (menuIcon) menuIcon.setAttribute('aria-expanded', 'true');
  }

  function closeNav(e) {
    if (e) e.preventDefault();
    if (!sidenav) return;
    sidenav.style.width = '0';
    if (menuIcon) menuIcon.setAttribute('aria-expanded', 'false');
  }

  if (menuIcon) {
    menuIcon.addEventListener('click', openNav);
    // 키보드 접근성: Enter/Space로도 열기
    menuIcon.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') openNav(ev);
    });
  }

  if (closeBtn) closeBtn.addEventListener('click', closeNav);


  // 사용자 정보/메뉴 표시
  fetchUserInfo();

  // 페이지 구분
  const isConfigPage    = document.body.classList.contains("page-config");
  const isRecordingPage = document.body.classList.contains("page-recording");
  const isChannelPage   = document.body.classList.contains("page-channels");
  const isCookiesPage   = document.body.classList.contains("page-cookies");
  const isFilesPage     = document.body.classList.contains("page-files");

  // 1) 설정 페이지
  if (isConfigPage) {

    // 분할옵션 ON/OFF 따라 하위옵션 활성화
	reflectSplitUI(); 
    document.getElementById('splitRecordingMode')?.addEventListener('change', reflectSplitUI);

    // 트레이 옵션 초기값 주입
    const selTray = document.getElementById('enableTray');
    if (selTray && window.appConfig && typeof window.appConfig.enableTray !== 'undefined') {
      selTray.value = (window.appConfig.enableTray ? 'true' : 'false');
  }

    setupConfigFormListeners();
    setupConfigFileManagerUI();
    setupAccountFormListeners();


    // 알림 테스트 버튼 핸들러
    setupNotificationTestButtons();

	// 인코딩 프리셋
	 setupPresetUI();
  
  }

  // 2) 녹화 현황 페이지
  if (isRecordingPage) {
    setupRecordingButtons();

	// DOM 안정화 직후 1회 + 주기적 집계 폴링
	queueMicrotask(() => { refreshAllStatus(); });
	setInterval(refreshAllStatus, 12000);

    // 썸네일 주기 갱신 (즉시 1회 + 5분마다)
    updateAllThumbnails();
    setInterval(updateAllThumbnails, 300000);

    // 필터/검색
    setupFilterListeners();
  }

  // 3) 채널 관리 페이지
  if (isChannelPage) {
    updateQualityAndExtensionOptions();

    // 플랫폼 변경 시 옵션 갱신
    const platformSel = document.getElementById('platform');
    if (platformSel) platformSel.addEventListener('change', updateQualityAndExtensionOptions);

    setupChannelFormListeners();
  }

  // 4) 쿠키 관리 페이지
  if (isCookiesPage) {
    setupCookieFormListeners && setupCookieFormListeners();
  }

  // 5) 파일 관리 페이지
	if (isFilesPage) {
	  setupFilesPage();

	}

  // 로그인 폼 리스너
  setupLoginFormListener();

  // 채널별 반복녹화 토글(공통)
  document.querySelectorAll('.toggle-record-enabled').forEach(checkbox => {
    checkbox.addEventListener('change', async (e) => {
      // 1) 채널 ID 회수
      let channelId = e.target.dataset.channelId;
      if (!channelId) {
        const host = e.target.closest('.channel');
        channelId = host && host.dataset.channelId;
      }
      if (!channelId) {
        console.error('[toggle] 채널 ID를 찾을 수 없습니다.');
        e.target.checked = !e.target.checked; // 롤백
        return;
      }

      const prev = e.target.checked; // 사용자가 건드린 값
      try {
		const res = await fetchWithTimeout(`/api/toggle_record_enabled/${channelId}`, {
		  method: 'POST',
		  headers: {
			'Content-Type': 'application/json',
			'Accept': 'application/json'
		  }
		}, 8000);

        if (!res.ok) {
          console.error('[toggle] 응답 오류', res.status);
          e.target.checked = !prev;
          return;
        }

        let data;
        try { data = await res.json(); }
        catch (err) {
          console.error('[toggle] JSON 파싱 오류(리다이렉트 응답 가능성)', err);
          e.target.checked = !prev;
          return;
        }

        if (data.status !== 'success') {
          console.error('[toggle] API 실패', data);
          e.target.checked = !prev;
          return;
        }

        // 2) 서버 상태 동기화
        const enabled = !!data.record_enabled;
        e.target.checked = enabled;

        // 3) 카드 스타일
		const card = document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`);
		if (card) card.classList.toggle('channel-disabled', !enabled);
      } catch (err) {
        console.error('[toggle] 호출 중 오류', err);
        e.target.checked = !prev;
      }
    });
  });
});


// 사용자/인증 관련
async function fetchUserInfo() {
  try {
    const response = await fetch('/user_info');
    const data = await response.json();

    const usernameDisplay = document.getElementById('username-display');
    const logoutBtn = document.getElementById('logout-btn');
    const restrictedMenuItems = document.querySelectorAll('.restricted-menu');

	// 설정 페이지에 트레이 사용 옵션 값 반영
	const trayWebSel = document.getElementById('enableTray');
	if (trayWebSel && data && data.config && Object.prototype.hasOwnProperty.call(data.config, 'enableTray')) {
	  trayWebSel.value = (data.config.enableTray ? 'true' : 'false');
	}

    if (data.config.loginMode) {
      if (data.logged_in) {
        if (usernameDisplay) usernameDisplay.textContent = `로그인: ${data.username}`;
        if (logoutBtn) logoutBtn.style.display = 'block';
        restrictedMenuItems.forEach(item => item.style.display = 'block');
      } else {
        if (usernameDisplay) usernameDisplay.textContent = '로그인되지 않음';
        if (logoutBtn) logoutBtn.style.display = 'none';
        restrictedMenuItems.forEach(item => item.style.display = 'none');
      }
    } else {
      if (usernameDisplay) usernameDisplay.textContent = '로그인 모드 OFF';
      if (logoutBtn) logoutBtn.style.display = 'none';
      restrictedMenuItems.forEach(item => item.style.display = 'block');
    }
  } catch (error) {
    console.error('사용자 정보를 가져오는 중 오류 발생:', error);
  }
}


// 로그인 폼 제출 처리
function setupLoginFormListener() {
  const loginForm = document.getElementById('loginForm');
  if (!loginForm) return;

  loginForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    const formData = new FormData(this);

    try {
      const response = await fetch('/login', { method: 'POST', body: formData });
      const result = await response.json();

      if (response.ok) {
        alert(result.message || "로그인 성공");
        const redirectUrl = result.redirect_url || '/';
        setTimeout(() => { window.location.href = redirectUrl; }, 2000);
      } else {
        alert(result.message || "아이디 또는 비밀번호가 올바르지 않습니다.");
      }
    } catch (error) {
      console.error("로그인 중 오류 발생:", error);
      alert("서버와의 통신 중 문제가 발생했습니다.");
    }
  });
}


// status 집계 폴링 
async function refreshAllStatus() {
  if (isRefreshingStatus) return;
  isRefreshingStatus = true;

  try {
    const res = await fetchWithTimeout('/status', {
      cache: 'no-store',
      credentials: 'same-origin'
    }, 8000);

    if (!res.ok) throw new Error('status fetch failed');
    const raw = await res.json();

    const entries = Array.isArray(raw)
      ? raw.map(x => [String(x.id || x.channel_id || x.channelId), x])
      : Object.entries(raw);

    for (const [cid, st] of entries) {
      if (!cid) continue;

      const recording = !!(st.recording ?? st.is_recording ?? (st.state === 'recording'));
      const reserved  = !!(st.reserved  ?? st.is_reserved  ?? (st.state === 'reserved'));
      const stopReq   = !!(st.stop_requested ?? st.stopRequested);
      const name      = st.name || st.channel_name || st.channelName || null;

      let state;
      if (recording) state = '녹화 중';
      else if (reserved) state = '예약녹화 중';
      else state = '대기 중';

      const duration = st.duration || st.recording_duration || (recording ? '00:00:00' : '00:00:00');
      const filename = st.filename || st.recording_filename
        || (st.output_path ? String(st.output_path).split(/[\\/]/).pop() : '')
        || (recording ? '(임시 파일명)' : '녹화 파일이 없습니다.');

      updateChannelStatusWithPriority(cid, state, filename, duration, stopReq, name, 1);
    }
  } catch (e) {
    console.warn('[WEB] refreshAllStatus error:', e);
  } finally {
    isRefreshingStatus = false;
  }
}


// 모든 채널 상태 병렬 체크
async function checkAllStatuses() {
  const channels = Array.from(document.querySelectorAll('.channel'));
  const tasks = channels.map(ch => {
    const id = ch.dataset.channelId;
    const pf = ch.dataset.platform;
    return checkStatus(id, pf).catch(err => {
      console.error(`[checkAll] ${id} 실패`, err);
    });
  });
  await Promise.allSettled(tasks);
}


// 특정 채널 상태 확인
async function checkStatus(channelId, platform) {
  try {
    const response = await fetchWithTimeout(`/api/check_status/${channelId}`, {
      method: 'GET'
    }, 8000);

    if (response.status === 404) {
      if (pollTimers[channelId]) {
        clearInterval(pollTimers[channelId]);
        delete pollTimers[channelId];
      }
      document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`)?.remove();
      console.log(`[INFO] 채널 ${channelId} 삭제 → 폴링 중단`);
      return;
    }

    if (!response.ok) throw new Error(`[DEBUG] status HTTP: ${response.status}`);

    const data = await response.json();
    if (data.status === 'success') {
      // 파일명 키 호환 보정
      if (!data.filename) {
        data.filename = data.recording_filename || (data.output_path ? String(data.output_path).split(/[\\/]/).pop() : '');
      }

      if (data.state === "녹화 중") {
        if ((data.recording_duration === "00:00:00" || (data.recording_duration || '').trim() === "") && lastRecordingDuration[channelId]) {
          data.recording_duration = lastRecordingDuration[channelId];
        } else {
          lastRecordingDuration[channelId] = data.recording_duration;
        }
        if ((data.filename === "녹화 파일이 없습니다." || (data.filename || '').trim() === "") && lastRecordingFilename[channelId]) {
          data.filename = lastRecordingFilename[channelId];
        } else {
          lastRecordingFilename[channelId] = data.filename;
        }
      }
      updateChannelStatusWithPriority(channelId, data.state, data.filename, data.recording_duration, data.stop_requested, null, 1);
    } else {
      console.error(`[DEBUG] Failed to check status for channel: ${channelId}`, data);
    }
  } catch (error) {
    console.error(`[DEBUG] Error checking status for channel: ${channelId}`, error);
  }
}


// 우선순위 기반 적용 래퍼
function updateChannelStatusWithPriority(channelId, state, filename, recordingDuration, stopRequested, channelNameFromServer = null, priority = 0) {
  const cur = (statusPriority[channelId] ?? -1);
  if (priority < cur) return;     // 더 낮은 우선순위면 무시
  statusPriority[channelId] = priority;
  updateChannelStatus(channelId, state, filename, recordingDuration, stopRequested, channelNameFromServer);
}


// 채널 카드 상태 업데이트
function updateChannelStatus(channelId, state, filename, recordingDuration, stopRequested, channelNameFromServer = null) {
  const channelElement = document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`);
  if (!channelElement) return;

  const statusElement = channelElement.querySelector('.channel-name');
  const filenameElement = channelElement.querySelector(`#filename-${channelId}`);
  const timingElement = channelElement.querySelector(`#timing-${channelId}`);

  const channelName = channelNameFromServer || (statusElement ? statusElement.dataset.channelName : 'Unknown Channel');

  if (statusElement) statusElement.innerText = `${channelName} (${state})`;
  if (filenameElement) filenameElement.innerText = `녹화 파일명: ${filename}`;

  if (timingElement) {
    if (state === "녹화 중") {
      timingElement.innerHTML = `<strong>녹화 시간</strong>: ${recordingDuration || '00:00:00'}`;
    } else if (state === "예약녹화 중") {
      timingElement.innerHTML = `<strong>예약녹화 중</strong>`;
    } else {
      timingElement.innerHTML = `<strong>녹화 상태</strong>: 대기 중`;
    }
  }

  const startBtn = document.getElementById(`button-start-${channelId}`);
  const stopBtn = document.getElementById(`button-stop-${channelId}`);

  if (state === "녹화 중" || state === "예약녹화 중") {
    if (startBtn) startBtn.style.display = 'none';
    if (stopBtn) {
      stopBtn.style.display = 'inline-block';
    } else {
      const newStopBtn = document.createElement('button');
      newStopBtn.id = `button-stop-${channelId}`;
      newStopBtn.classList.add('stop-recording');
      newStopBtn.dataset.channelId = channelId;
      newStopBtn.innerText = '녹화 중지';
      channelElement.appendChild(newStopBtn);
    }
  } else {
    if (startBtn) {
      startBtn.style.display = 'inline-block';
    } else {
      const newStartBtn = document.createElement('button');
      newStartBtn.id = `button-start-${channelId}`;
      newStartBtn.classList.add('start-recording');
      newStartBtn.dataset.channelId = channelId;
      newStartBtn.innerText = '녹화 시작';
      channelElement.appendChild(newStartBtn);
    }
    if (stopBtn) stopBtn.style.display = 'none';
  }
}


// 채널카드에 메타/썸네일 반영
function applyMetaToCard(id, platform, meta = {}) {
  // 카드 찾기(특수문자 채널ID 보호)
  const card = document.querySelector(`.channel[data-channel-id="${cssEsc(id)}"]`);
  if (!card) return;

  const pf = (platform || card.dataset.platform || '').toLowerCase();
  const $ = sel => card.querySelector(sel);
  const byId = domId => document.getElementById(domId) || $(`#${cssEsc(domId)}`);

  const nowTs = Date.now();

  // 썸네일
  const thumbEl = byId(`thumbnail-${id}`) || $('img.thumbnail, .thumb img');
  if (thumbEl) {
    const url = meta.thumbnail_url || meta.thumbnailUrl || meta.thumbnail;
    const isLive = meta.is_live === true || meta.isLive === true || meta.live === true || meta.status === 'OPEN';
    const fallback = pf === 'cime'
      ? (isLive ? `/static/img/cime_thumbnail.png?t=${nowTs}` : `/static/img/cimeclosed_thumbnail.png?t=${nowTs}`)
      : `/static/img/default_thumbnail.png?t=${nowTs}`;
    thumbEl.src = url ? `${url}?t=${nowTs}` : fallback;
  }

  // 제목 / 카테고리 / 성인표시
  const titleEl = byId(`title-${id}`) || $('.channel-title, .title');
  if (titleEl) {
    const t = meta.live_title || meta.title || meta.video_title || meta.name || '방송 제목 없음';
    titleEl.textContent = meta.adult ? `${t} (연령제한)` : t;
  }

  const catEl = byId(`category-${id}`) || $('.channel-category, .category');
  if (catEl) {
    const c =
      meta.category || meta.category_name || meta.game || meta.game_name || meta.genre || '카테고리 없음';
    catEl.innerHTML = `<strong>카테고리</strong>: ${c}`;
  }

  // 녹화 시간(라이브일 때만)
  const timingEl = byId(`timing-${id}`) || $('.channel-timing, .timing');
  if (timingEl && (meta.is_live === true || meta.isLive === true || meta.live === true)) {
    const dur = meta.recording_duration || '00:00:00';
    timingEl.innerHTML = `<strong>녹화 시간</strong>: ${dur}`;
  }
}


// 카드별 개별 메타 보강
async function fetchMetaPerChannel(channelId) {
  try {
    const r = await fetchWithTimeout(`/api/update_metadata/${channelId}`, {}, 8000);
    if (!r.ok) return null;
    const j = await r.json();
    return (j && j.status === 'success') ? (j.metadata || {}) : null;
  } catch { return null; }
}


// 모든 썸네일/메타 갱신
async function updateAllThumbnails() {
  if (isThumbJobRunning) return;
  isThumbJobRunning = true;

  try {
    const nowTs = Date.now();
    const cards = Array.from(document.querySelectorAll('.channel'));
    const cardById = new Map(cards.map(c => [String(c.dataset.channelId), c]));

    // 카드에 메타 적용 
    function applyMeta(id, platform, meta, root) {
      const card = cardById.get(id);
      if (!card) return;

      const sel  = (q) => card.querySelector(q);
      const byId = (domId) => document.getElementById(domId) || sel(`#${domId}`);

      const thumbEl  = byId(`thumbnail-${id}`) || sel('img.thumbnail, .thumb img');
      const titleEl  = byId(`title-${id}`)     || sel('.channel-title, .title');
      const catEl    = byId(`category-${id}`)  || sel('.channel-category, .category');
      const timingEl = byId(`timing-${id}`)    || sel('.channel-timing, .timing');

      // 썸네일
      const thumbUrl = pick(meta, ['thumbnail_url','thumbnailUrl','thumbnail'])
                    ?? pick(root, ['thumbnail_url','thumbnailUrl','thumbnail']);
      // 텍스트 메타
      const title    = pick(meta, ['live_title','liveTitle','title','video_title','name']);
      const category = pick(meta, ['category','category_name','game','game_name','genre','tag']);
      const isLive   = pick(meta, ['is_live','isLive','live','online']) === true || pick(meta, ['status']) === 'OPEN';

      if (thumbEl) {
        const pf = (platform || card.dataset.platform || '').toLowerCase();
        const fallback = pf === 'cime'
          ? (isLive ? `/static/img/cime_thumbnail.png?t=${nowTs}` : `/static/img/cimeclosed_thumbnail.png?t=${nowTs}`)
          : `/static/img/default_thumbnail.png?t=${nowTs}`;
        thumbEl.src = thumbUrl ? `${thumbUrl}?t=${nowTs}` : fallback;
      }
      const adult    = !!meta.adult;

      if (titleEl && title) {
        titleEl.textContent = adult ? `${title} (연령제한)` : title;
      }
      if (catEl && category) {
        catEl.innerHTML = `<strong>카테고리</strong>: ${category}`;
      }
      if (timingEl && isLive) {
        const dur = meta.recording_duration || '00:00:00';
        timingEl.innerHTML = `<strong>녹화 시간</strong>: ${dur}`;
      }
    }

    // (A) 벌크 상태 수집 & 1차 DOM 반영 
    let bulkItems = [];
    try {
  	  const res = await fetchWithTimeout('/api/thumbnail_status', {
	    headers: { 'Accept': 'application/json' },
	    cache: 'no-store'
	  }, 10000);
      if (res.ok) {
        const raw  = await res.json();
        const list = Array.isArray(raw) ? raw
                  : Array.isArray(raw?.channels) ? raw.channels
                  : Array.isArray(raw?.items)    ? raw.items
                  : Array.isArray(raw?.data)     ? raw.data
                  : [];
        bulkItems = list.map(x => {
          const id = x.channel_id ?? x.id ?? x.channelId ?? x.channel?.id ?? x.meta?.id ?? '';
          const pf = x.platform ?? x.pf ?? x.meta?.platform ?? x.metadata?.platform ?? '';
          const meta = (x.metadata && typeof x.metadata === 'object') ? x.metadata
                    : (x.meta && typeof x.meta === 'object') ? x.meta
                    : x;
          return { id: String(id || ''), platform: String(pf || ''), meta, root: x };
        }).filter(v => v.id);
      }
    } catch {/* 벌크 실패는 조용히 무시 */}

    // 벌크 1차 반영
    const bulkMap = new Map();
    for (const it of bulkItems) {
      bulkMap.set(it.id, it);
      applyMeta(it.id, it.platform, it.meta, it.root);
    }

    // (B) 보강 대상 선별(텍스트 메타가 비었고 쿨타임 지난 것만)
    const needs = [];
    for (const [id, card] of cardById.entries()) {
      const bulk = bulkMap.get(id);
      const meta = bulk?.meta || {};
      const title    = pick(meta, ['live_title','liveTitle','title','video_title','name']);
      const category = pick(meta, ['category','category_name','game','game_name','genre','tag']);
      const isLive   = pick(meta, ['is_live','isLive','live','online']);

      const missingText = (!title && !category && (isLive === undefined || isLive === null));
      if (missingText && (Date.now() - (lastMetaFetchAt[id] || 0) > META_COOLDOWN_MS)) {
        lastMetaFetchAt[id] = Date.now();
        needs.push({ id });
      }
    }

    // (C) 동시성 제한 보강 호출
    await mapLimit(needs, MAX_CONCURRENCY, async ({ id }) => {
      const patched = await fetchMetaPerChannel(id);  // /api/update_metadata/{id}
      if (!patched) return;
      const pf = (cardById.get(id)?.dataset.platform) || '';
      applyMeta(id, pf, patched, null);
    });

  } catch (e) {
    console.error('updateAllThumbnails error', e);
  } finally {
    isThumbJobRunning = false;
  }
}



// 필터링 드롭다운/검색 리스너
function setupFilterListeners() {
  const filterDropdown = document.getElementById('channel-filter');
  const searchInput = document.getElementById('channel-search');

  if (filterDropdown) filterDropdown.addEventListener('change', applyFilters);
  if (searchInput) searchInput.addEventListener('input', debounce(applyFilters, 300));
}


// 채널 필터 helper
function filterChannels(condition) {
  const channels = document.querySelectorAll('.channel');
  channels.forEach(channel => {
    channel.style.display = condition(channel) ? 'block' : 'none';
  });
}


// 드롭다운 + 검색어 동시 적용
function applyFilters() {
  const filterValue = document.getElementById('channel-filter').value;
  const searchValue = document.getElementById('channel-search').value.toLowerCase();

  filterChannels(channel => {
    const statusText = channel.querySelector('.channel-name').textContent;
    const channelName = channel.querySelector('.channel-name').textContent.toLowerCase();

    const matchesFilter = (
      filterValue === 'all' ||
      (filterValue === 'recording' && statusText.includes('녹화 중') && !statusText.includes('예약녹화 중')) ||
      (filterValue === 'reserved' && statusText.includes('예약녹화 중'))
    );
    const matchesSearch = channelName.includes(searchValue);

    return matchesFilter && matchesSearch;
  });
}


// 녹화 버튼 바인딩
function setupRecordingButtons() {
  const channelList = document.getElementById('channel-list');
  if (channelList) {
    channelList.addEventListener('click', function (event) {
      const startButton = event.target.closest('.start-recording');
      const stopButton = event.target.closest('.stop-recording');

      if (startButton) startRecording(startButton.dataset.channelId);
      if (stopButton)  stopRecording(stopButton.dataset.channelId);
    });
  }

  const startAllBtn = document.getElementById('start-all-recording');
  if (startAllBtn) {
    startAllBtn.addEventListener('click', function () {
      const channelIds = Array.from(document.querySelectorAll('.channel')).map(c => c.dataset.channelId);
      if (channelIds.length > 0) startAllRecordings(channelIds);
    });
  }

  const stopAllBtn = document.getElementById('stop-all-recording');
  if (stopAllBtn) {
    stopAllBtn.addEventListener('click', function () {
      const channelIds = Array.from(document.querySelectorAll('.channel')).map(c => c.dataset.channelId);
      if (channelIds.length > 0) stopAllRecordings(channelIds);
    });
  }
}


// 개별 채널 녹화 시작
async function startRecording(channelId) {
  if (isStartingRecordings) return;
  isStartingRecordings = true;

  const apiUrl = `/api/start_recording/${channelId}`;
  try {
    const response = await fetchWithTimeout(apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_user_request: true })
    }, 12000);

    // 에러 응답 처리
    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || '녹화 시작 요청에 실패했습니다.');
    }

    // 성공 응답 처리
    const data = await response.json().catch(() => ({}));
    if (data.status === 'success') {
      const st   = data.state || '녹화 중';
  	  const file = data.filename || data.recording_filename
			    || (data.output_path ? String(data.output_path).split(/[\\/]/).pop() : '')
			    || (st === '녹화 중' ? '(임시 파일명)' : '녹화 파일이 없습니다.');
      const dur  = st === '녹화 중' ? (data.recording_duration || '00:00:00') : '00:00:00';

      updateChannelStatusWithPriority(channelId, st, file, dur, false, null, 1);

      // 최신 서버 상태로 한 번 더 동기화
      const el = document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`);
      await checkStatus(channelId, el ? el.dataset.platform : undefined);
    } else {
      console.error('녹화 시작 실패:', data);
    }
  } catch (error) {
    console.error(`녹화 시작 오류 - 채널 ID: ${channelId}:`, error);
  } finally {
    isStartingRecordings = false;
  }
}


// 개별 채널 녹화 중지
async function stopRecording(channelId) {
  const apiUrl = `/api/stop_recording/${channelId}`;
  try {
    const response = await fetchWithTimeout(apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    }, 12000);

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.detail || '녹화 중지 요청에 실패했습니다.');
    }

    const data = await response.json();
    if (data.status === 'success') {
      updateChannelStatusWithPriority(channelId, "대기 중", "녹화 파일이 없습니다.", "00:00:00", false, null, 1);
      const el2 = document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`);
      await checkStatus(channelId, el2 ? el2.dataset.platform : undefined);
    } else {
      console.error('녹화 중지 실패:', data);
    }
  } catch (error) {
    console.error(`녹화 중지 오류 - 채널 ID: ${channelId}:`, error);
  }
}


// 모든 채널 녹화 시작
async function startAllRecordings(channelIds, platforms) {
  if (isStartingRecordings) { console.log("이미 녹화 시작 중입니다."); return; }
  isStartingRecordings = true;
  try {
    if (!channelIds || channelIds.length === 0) throw new Error("녹화를 시작할 채널이 없습니다.");
    const response = await fetchWithTimeout(`/api/start_all_recording`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channelIds, platforms, is_user_request: true })
    }, 15000);
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || '모두 녹화 시작 요청 실패');
    }
    const data = await response.json();
    if (data.status === 'success') {
      Object.keys(data.channels_status).forEach(channelId => {
        const channelElement = document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`);
        if (channelElement) {
          updateChannelStatus(
            channelId,
            data.channels_status[channelId].state,
            data.channels_status[channelId].filename,
            data.channels_status[channelId].recording_duration || '00:00:00'
          );
        }
      });
    } else {
      console.error('모두 녹화 시작 실패:', data);
    }
  } catch (error) {
    console.error(`모두 녹화 시작 오류: ${error.message}`);
  } finally {
    isStartingRecordings = false;
  }
}


// 모든 채널 녹화 중지
async function stopAllRecordings(channelIds, platforms) {
  if (isStoppingRecordings) { console.log("이미 녹화 중지 중입니다."); return; }
  isStoppingRecordings = true;
  try {
    if (!channelIds || channelIds.length === 0) throw new Error("녹화를 중지할 채널이 없습니다.");
    const response = await fetchWithTimeout(`/api/stop_all_recording`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channelIds, platforms, is_user_request: true })
    }, 15000);
    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.detail || '모두 녹화 중지 요청 실패');
    }
    const data = await response.json();
    if (data.status === 'success') {
      Object.keys(data.channels_status).forEach(channelId => {
        const channelElement = document.querySelector(`.channel[data-channel-id="${cssEsc(channelId)}"]`);
        if (channelElement) {
          updateChannelStatus(
            channelId,
            data.channels_status[channelId].state,
            data.channels_status[channelId].filename,
            data.channels_status[channelId].recording_duration
          );
        }
      });
    } else {
      console.error('모두 녹화 중지 실패:', data);
    }
  } catch (error) {
    console.error(`모두 녹화 중지 오류: ${error.message}`);
  } finally {
    isStoppingRecordings = false;
  }
}


// 녹화현형황 대시보드
function _fmtBytes(n) {
  if (!Number.isFinite(n)) return "-";
  const u = ["B","KB","MB","GB","TB","PB"];
  let i = 0, x = n;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(2)} ${u[i]}`;
}


function _fmtRate(bytesPerSec) {
  if (!Number.isFinite(bytesPerSec)) return "0.00 MB/s";
  const mb = bytesPerSec / (1024*1024);
  return `${mb.toFixed(2)} MB/s`;
}


function _setBar(id, percent) {
  const el = document.getElementById(id);
  if (!el) return;
  const p = Math.max(0, Math.min(100, percent || 0));
  el.style.width = `${p}%`;
}

function _setMetricLevel(elementId, percent, warning = 70, danger = 90) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const value = Number(percent) || 0;
  el.dataset.level = value >= danger ? "danger" : value >= warning ? "warning" : "normal";
}


async function _fetchSys() {
  const r = await fetchWithTimeout('/api/sys_metrics', { cache: 'no-store' }, 5000);
  if (!r.ok) throw new Error('sys_metrics 요청 실패');
  return r.json();
}


function _renderMainTiles(m) {
  // CPU
  document.getElementById('cpu-name')?.replaceChildren(document.createTextNode(m?.cpu?.name || '-'));
  const cp = +(m?.cpu?.percent || 0);
  document.getElementById('cpu-percent')?.replaceChildren(document.createTextNode(cp.toFixed(0)));
  _setBar('cpu-bar', cp);
  _setMetricLevel('tile-cpu', cp);

  // MEM
  const mp = +(m?.memory?.percent || 0);
  document.getElementById('mem-percent')?.replaceChildren(document.createTextNode(mp.toFixed(0)));
  _setBar('mem-bar', mp);
  _setMetricLevel('tile-mem', mp);
  const tot = m?.memory?.total || 0;
  const used = m?.memory?.used || 0;
  document.getElementById('mem-brief')?.replaceChildren(
    document.createTextNode(`${_fmtBytes(used)} / ${_fmtBytes(tot)}`)
  );

  // NET (서버가 제공하는 즉시 속도 사용)
  const up   = +(m?.network?.up_bps || 0);
  const down = +(m?.network?.down_bps || 0);

  document.getElementById('net-rate')?.replaceChildren(
    document.createTextNode(`↑ ${_fmtRate(up)} · ↓ ${_fmtRate(down)}`)
  );
  const netScore = Math.min(100, (up + down) / (1024*1024) * 10); // 10MB/s=100%
  _setBar('net-bar', netScore);
  _setMetricLevel('tile-net', netScore, 65, 90);

  // 보조정보(누적 바이트는 서브 텍스트로만)
  const sent = m?.network?.bytes_sent || 0;
  const recv = m?.network?.bytes_recv || 0;
  document.getElementById('net-brief')?.replaceChildren(
    document.createTextNode(`누적 ↑${_fmtBytes(sent)} / ↓${_fmtBytes(recv)}`)
  );
}


function _ensureDiskRows() {
  const r1 = document.getElementById('disk-row-1');
  const r2 = document.getElementById('disk-row-2');
  if (!r1 || !r2) return [null, null];
  return [r1, r2];
}


function _mkDiskTile(idx, d) {
  const host = document.createElement('div');
  host.className = 'tile disk';

  const head = document.createElement('div');
  head.className = 'tile-head';
  const t = document.createElement('span');
  t.className = 'tile-title';
  t.textContent = (d?.label || d?.mountpoint || d?.device || `Disk ${idx+1}`);
  const sub = document.createElement('span');
  sub.className = 'tile-sub';
  sub.textContent = (d?.fstype ? d.fstype.toUpperCase() : '');
  head.appendChild(t); head.appendChild(sub);

  const val = document.createElement('div');
  val.className = 'tile-value';
  val.id = `disk-value-${idx}`;
  const pct = +(d?.percent || 0);
  val.textContent = `${pct.toFixed(0)}%`;

  const prog = document.createElement('div');
  prog.className = 'progress';
  const bar = document.createElement('div');
  bar.className = 'progress-bar';
  bar.id = `disk-bar-${idx}`;
  bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  prog.appendChild(bar);

  const brief = document.createElement('div');
  brief.className = 'tile-brief';
  brief.id = `disk-brief-${idx}`;
  brief.textContent = `${_fmtBytes(d?.used||0)} / ${_fmtBytes(d?.total||0)}`;

  host.appendChild(head);
  host.appendChild(val);
  host.appendChild(prog);
  host.appendChild(brief);
  return host;
}


function _renderDisks(arr) {
  const [r1, r2] = _ensureDiskRows();
  if (!r1 || !r2) return;
  const top10 = (arr || []).slice(0, 10);
  const signature = top10.map((d) => `${d?.mountpoint || ""}|${d?.label || ""}|${d?.fstype || ""}`).join(";");

  if (signature !== __sys_disk_signature) {
    __sys_disk_signature = signature;
    r1.replaceChildren();
    r2.replaceChildren();
    top10.forEach((d, i) => {
      const tile = _mkDiskTile(i, d);
      tile.id = `disk-tile-${i}`;
      if (i < 5) r1.appendChild(tile);
      else r2.appendChild(tile);
    });
  }

  top10.forEach((d, i) => {
    const pct = +(d?.percent || 0);
    document.getElementById(`disk-value-${i}`)?.replaceChildren(document.createTextNode(`${pct.toFixed(0)}%`));
    const bar = document.getElementById(`disk-bar-${i}`);
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    document.getElementById(`disk-brief-${i}`)?.replaceChildren(
      document.createTextNode(`${_fmtBytes(d?.used || 0)} / ${_fmtBytes(d?.total || 0)}`)
    );
    _setMetricLevel(`disk-tile-${i}`, pct, 75, 90);
  });

  document.getElementById('disk-count')?.replaceChildren(
    document.createTextNode(`${top10.length}개 드라이브`)
  );
}


async function _pollSys() {
  if (isPollingSys) return;
  isPollingSys = true;

  try {
    const m = await _fetchSys();
    _renderMainTiles(m);
    _renderDisks(m.disks || []);
    const updated = new Date((m.sampled_at || Date.now() / 1000) * 1000);
    document.getElementById('sys-updated')?.replaceChildren(
      document.createTextNode(`${updated.toLocaleTimeString('ko-KR')} 갱신`)
    );
  } catch (e) {
    document.getElementById('sys-updated')?.replaceChildren(
      document.createTextNode('측정값을 가져오지 못했습니다')
    );
  } finally {
    isPollingSys = false;
  }
}

function _scheduleSysPoll(delay = 3000) {
  if (__sys_poll_tm) clearTimeout(__sys_poll_tm);
  if (document.hidden || !__sys_dashboard_expanded) {
    __sys_poll_tm = null;
    return;
  }
  __sys_poll_tm = setTimeout(async () => {
    await _pollSys();
    _scheduleSysPoll(3000);
  }, delay);
}

function _setSysDashboardExpanded(expanded) {
  const dashboard = document.getElementById('sys-dashboard');
  const body = document.getElementById('sys-dashboard-body');
  const button = document.getElementById('sys-dashboard-toggle');
  if (!dashboard || !body || !button) return;

  __sys_dashboard_expanded = expanded;
  body.hidden = !expanded;
  dashboard.classList.toggle('is-collapsed', !expanded);
  button.textContent = expanded ? '접기' : '펼치기';
  button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  localStorage.setItem('recordweb.systemMonitorExpanded', expanded ? '1' : '0');

  if (expanded) {
    _pollSys();
    _scheduleSysPoll(3000);
  } else if (__sys_poll_tm) {
    clearTimeout(__sys_poll_tm);
    __sys_poll_tm = null;
  }
}


function setupSysDashboard() {
  const button = document.getElementById('sys-dashboard-toggle');
  if (!button) return;

  const saved = localStorage.getItem('recordweb.systemMonitorExpanded');
  _setSysDashboardExpanded(saved !== '0');
  button.addEventListener('click', () => {
    _setSysDashboardExpanded(!__sys_dashboard_expanded);
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (__sys_poll_tm) clearTimeout(__sys_poll_tm);
      __sys_poll_tm = null;
      return;
    }
    if (__sys_dashboard_expanded) {
      _pollSys();
      _scheduleSysPoll(3000);
    }
  });
}


// 페이지 진입 시 자동 초기화 (녹화현황)
document.addEventListener('DOMContentLoaded', () => {
  if (location.pathname.includes('/recording') && document.getElementById('sys-dashboard')) {
    setupSysDashboard();
  }
});


// 프리셋 세팅 함수
function setupPresetUI() {
  const presets = {
    libx264: ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow','placebo'],
    libx265: ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow','placebo'],
    h264_qsv: ['veryfast','faster','fast','medium','slow','slower'],
    hevc_qsv: ['veryfast','faster','fast','medium','slow','slower'],
    h264_nvenc: ['p1','p2','p3','p4','p5','p6','p7','default','slow','medium','fast','hp','hq','bd','ll','llhq','llhp','lossless','losslesshp'],
    hevc_nvenc: ['p1','p2','p3','p4','p5','p6','p7','default','slow','medium','fast','hp','hq','bd','ll','llhq','llhp','lossless','losslesshp'],
    h264_amf: ['balanced','speed','quality'],
    hevc_amf: ['balanced','speed','quality']
  };

  // GPU0
  const videoCodecSelect = document.getElementById('video_codec');
  const presetSelect     = document.getElementById('preset');

  // GPU1
  const videoCodecSelectGpu1 = document.getElementById('video_codec_gpu1');
  const presetSelectGpu1     = document.getElementById('preset_gpu1');

  const streamCopySel    = document.getElementById('stream_copy');

  // 공용 적용 함수(코덱셀렉트 + 프리셋셀렉트 + 저장프리셋 문자열)
  function applyPreset(codecSel, presetSel, savedStr) {
    if (!codecSel || !presetSel) return;

    const list = presets[codecSel.value] || [];
    presetSel.innerHTML = '';

    list.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      presetSel.appendChild(opt);
    });

    const want = (savedStr || '').trim().toLowerCase();
    const found = Array.from(presetSel.options).find(o => o.value.toLowerCase() === want);

    if (found) {
      presetSel.value = found.value;
    } else if (presetSel.options[0]) {
      presetSel.value = presetSel.options[0].value;
    }
  }

  function updatePresetOptions() {
    // GPU0
    applyPreset(videoCodecSelect, presetSelect, window.savedPreset);

    // GPU1
    applyPreset(videoCodecSelectGpu1, presetSelectGpu1, window.savedPresetGpu1);
  }

  function reflectAvailability() {
    const off = streamCopySel && streamCopySel.value === 'true';

    if (presetSelect) {
      presetSelect.disabled = !!off;
      presetSelect.title = off ? '스트림복사에서는 프리셋이 필요 없습니다.' : '';
    }
    if (presetSelectGpu1) {
      presetSelectGpu1.disabled = !!off;
      presetSelectGpu1.title = off ? '스트림복사에서는 프리셋이 필요 없습니다.' : '';
    }
  }

  updatePresetOptions();
  reflectAvailability();

  videoCodecSelect?.addEventListener('change', updatePresetOptions);
  videoCodecSelectGpu1?.addEventListener('change', updatePresetOptions);
  streamCopySel?.addEventListener('change', reflectAvailability);
}


// 분할녹화 설정 반영 
function reflectSplitUI() {
  const toggleEl  = document.getElementById('splitRecordingMode'); 
  const secsEl    = document.getElementById('autoStopInterval');
  const overlapEl = document.getElementById('splitOverlapSec');
  const splitPPEl = document.getElementById('splitPostProcessing');

  if (!toggleEl || !secsEl || !overlapEl) return;

  const val = (toggleEl.type === 'checkbox')
    ? toggleEl.checked
    : String(toggleEl.value).toLowerCase();
  const on = (val === true) || (val === 'true') || (val === 'on') || (val === '1');

  secsEl.disabled     = !on;
  overlapEl.disabled  = !on;

  // 분할녹화 전용 후처리도 함께 토글
  if (splitPPEl) splitPPEl.disabled = !on;

  // 기본값 채우기
  if (on) {
    if (!secsEl.value || secsEl.value === '0') secsEl.value = '3600';
    if (!overlapEl.value) overlapEl.value = '8';

    // 분할 후처리 기본값: ON
    if (splitPPEl && (splitPPEl.value === '' || splitPPEl.value == null)) {

      // select/checkbox 둘 다 대응
      if (splitPPEl.tagName === 'SELECT') {
        splitPPEl.value = 'true';
      } else if (splitPPEl.type === 'checkbox') {
        splitPPEl.checked = true;
      }
    }
  }
}


// 설정 폼 리스너 설정
function setupConfigFormListeners() {
  const configForm = document.getElementById('configForm');
  if (!configForm) return;

  // [GPU UI] gpuCount 변경 시 GPU1 프로필/채널 분배 섹션 즉시 토글
  const gpuCountSelect = document.getElementById('gpuCount');
  const gpu1ProfileSection = document.getElementById('gpu1ProfileSection');
  const gpuAssignSection = document.getElementById('gpuAssignSection');
  const gpuAssignmentsHidden = document.getElementById('gpuAssignmentsJson');

  function updateGpuSectionsVisibility() {
    if (!gpuCountSelect) return;
    const gpuCount = parseInt(gpuCountSelect.value || '1', 10) || 1;
    const show = (gpuCount === 2);

    if (gpu1ProfileSection) gpu1ProfileSection.style.display = show ? 'block' : 'none';
    if (gpuAssignSection)  gpuAssignSection.style.display  = show ? 'block' : 'none';


    // 단일 GPU로 내렸을 때 hidden 값은 비워두기(제출 시에도 submit 핸들러가 delete 처리하지만, UI 혼동 방지)
    if (!show && gpuAssignmentsHidden) gpuAssignmentsHidden.value = '';
  }

  if (gpuCountSelect) {
    gpuCountSelect.addEventListener('change', updateGpuSectionsVisibility);
    gpuCountSelect.addEventListener('input',  updateGpuSectionsVisibility);
    updateGpuSectionsVisibility(); // 페이지 진입 시 1회 동기화
  }

  configForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (isSubmitting) return;

    const splitToggle  = document.getElementById('splitRecordingMode');  // 체크박스
    const splitSecs    = document.getElementById('autoStopInterval');    // 분할 시간(초)
    const overlapInput = document.getElementById('splitOverlapSec');     // 오버랩(초) - 신규 필드

    // 로그인모드 + 계정없음 → 등록 페이지 유도
    const loginModeOn = (this.querySelector('#loginMode')?.value === 'true');
    const hasAccount  = (this.dataset.hasAccount === '1');
    if (loginModeOn && !hasAccount) {
      alert('로그인 모드를 켜려면 먼저 계정을 생성하세요.\n계정 생성 페이지로 이동합니다.');
      window.location.href = '/register?need_account=1';
      return;
    }

    isSubmitting = true;
    try {
      const formData = new FormData(this);

      // 트레이 셀렉트가 없거나 disabled로 누락되었을 때 기본 false 보정
      if (!formData.has('enableTray')) formData.append('enableTray', 'false');

      // gpuCount를 먼저 확보(아래 GPU 분배/추가키 보정에 재사용)
      const gpuCountEl = document.getElementById('gpuCount');
      const gpuCount = parseInt((gpuCountEl && gpuCountEl.value) ? gpuCountEl.value : '1', 10) || 1;

      // 후처리 출력 크기 드롭다운이 disabled면 FormData에서 빠질 수 있음 → 현재 값으로 보정
      const ppRes0El = document.getElementById('postprocess_resolution');
      if (!formData.has('postprocess_resolution') && ppRes0El) {
        formData.append('postprocess_resolution', ppRes0El.value || 'source');
      }
      if (gpuCount === 2) {
        const ppRes1El = document.getElementById('postprocess_resolution_gpu1');
        if (!formData.has('postprocess_resolution_gpu1') && ppRes1El) {
          formData.append(
            'postprocess_resolution_gpu1',
            ppRes1El.value || (ppRes0El ? (ppRes0El.value || 'source') : 'source')
          );
        }
      }

      // 1) disabled 때문에 빠질 수 있는 값 보정
      if (!formData.has('timemachine_time_shift')) formData.append('timemachine_time_shift', '0');

      // 2) 체크 안 된 체크박스/셀렉트 기본값 보정
      [
        'autoPostProcessing','deleteAfterPostProcessing','removeFixedPrefix',
        'moveAfterProcessingEnabled','postNewWindow', 'stream_copy',
        'use_bitrate_mode','splitRecordingMode','fileManagerEnabled',
        'loginMode','fileManagerReadOnly','trashEnabled', 'enableTray'

      ].forEach(name => {
        if (!formData.has(name)) formData.append(name, 'false');
      });

      // fileManagerMode 기본값 보정 (blacklist|whitelist)
      const fmMode = (formData.get('fileManagerMode') || '').trim();
      if (!fmMode || (fmMode !== 'blacklist' && fmMode !== 'whitelist')) {
        formData.set('fileManagerMode', 'blacklist');
      }

      // 3) 숫자 선택사항은 빈 문자열이면 제거 (int 변환 실패 방지)
      if ((formData.get('autoStopInterval') || '') === '') formData.delete('autoStopInterval');

      // 4) 옵션 문자열들 기본값 보정
      [
        'moveAfterProcessing', 'vbv_maxrate','vbv_bufsize', 'extra_ffmpeg_options',
		'telegram_enabled','telegram_bot_token','telegram_chat_id',
		'discord_enabled','discord_webhook_url',
		'notify_record_started','notify_record_finished','notify_record_start_failed',
		'notify_record_abnormally_stopped','notify_record_user_stopped',
		'notify_postprocess_finished','notify_postprocess_failed',
		'notify_cookie_auth_failed','notify_watchparty_skipped',
		'notify_disk_space_low','notify_dedupe_seconds','notify_disk_space_low_gb'

      ].forEach(name => {
        if (!formData.has(name)) formData.append(name, '');
      });

      if ((formData.get('splitOverlapSec') || '') === '') {
        // 빈 값이면 제거 (서버에서 0 처리)
        formData.delete('splitOverlapSec');
      } else {
        const ovRaw = parseInt(formData.get('splitOverlapSec'), 10);
        // 서버와 동일한 가드(0~30초 권장)
        if (!Number.isInteger(ovRaw) || ovRaw < 0) {
          formData.set('splitOverlapSec', '0');
        } else if (ovRaw > 30) {
          formData.set('splitOverlapSec', '30');
        }
      }

      // GPU 채널 분배(hidden) 구성: gpuCount==2일 때만 전송
      formData.delete('gpuAssignmentsJson'); // 혹시 이전 값이 들어가 있었으면 제거

      if (gpuCount === 2) {
        const mapping = {};

        document.querySelectorAll('[data-gpu-assign][data-channel-id]').forEach((sel) => {
          const cid = sel.getAttribute('data-channel-id');
          if (!cid) return;

          const vRaw = parseInt(sel.value || '0', 10);
          const gi = (vRaw === 1 ? 1 : 0);
          mapping[cid] = gi;
        });

        formData.set('gpuAssignmentsJson', JSON.stringify(mapping));
      }

      const response = await fetch('/config', { method: 'POST', body: formData });
      if (response.ok) {
        alert('설정이 성공적으로 저장되었습니다!');
        window.location.replace('/');
      } else {
        const text = await response.text();
        console.error('POST /config failed:', response.status, text);
        alert('설정 저장 중 오류가 발생했습니다.');
      }
    } catch (err) {
      console.error('에러 발생:', err);
      alert('설정 저장 중 문제가 발생했습니다.');
    } finally {
      isSubmitting = false;
    }
  });
}


// 계정 수정/삭제 폼 리스너 설정
function setupAccountFormListeners() {
  const updateAccountForm = document.getElementById("updateAccountForm");
  const deleteAccountForm = document.querySelector(".delete-form");

  if (updateAccountForm) {
    // 계정 수정
    updateAccountForm.addEventListener("submit", function (event) {
      event.preventDefault();

      const currentPassword = document.getElementById("current_password").value;
      const newPassword = document.getElementById("new_password").value;
      const newPasswordConfirm = document.getElementById("new_password_confirm").value;

      if (validatePassword(currentPassword, newPassword, newPasswordConfirm)) {
        submitForm(updateAccountForm, "계정이 성공적으로 수정되었습니다.");
      }
    });
  }

  if (deleteAccountForm) {
    // 계정 삭제
    deleteAccountForm.addEventListener("submit", function (event) {
      event.preventDefault();

      if (!isConfirmed) {
        const currentPassword = document.getElementById("delete-current_password").value;
        if (validatePassword(currentPassword)) {
          const confirmation = confirm("정말 계정을 삭제하시겠습니까?");
          if (confirmation) {
            isConfirmed = true;
            submitForm(deleteAccountForm, "계정이 삭제되었습니다.", "/logout");
          }
        }
      }
    });
  }
}


// 공통 폼 제출 래퍼
async function submitForm(formElement, successMessage, redirectUrl) {
  if (isSubmitting) return;
  isSubmitting = true;

  try {
    const formData = new FormData(formElement);
    const action = formData.get("action");

    // 삭제 액션일 땐 새 비번 필드 제외
    if (action === "delete") {
      formData.delete("new_password");
      formData.delete("new_password_confirm");
    }

    const response = await fetch("/updateAccount", {
      method: "POST",
      body: formData,
    });

    const result = await response.json();

    if (!response.ok) {
      alert(result.message || "오류가 발생했습니다.");
      return;
    }

    if (result.status === "success") {
      alert(successMessage || result.message);
      setTimeout(() => {
        window.location.href = redirectUrl || result.redirect_url;
      }, 2000);
    } else {
      alert(result.message || "오류가 발생했습니다.");
    }
  } catch (error) {
    console.error("폼 제출 중 오류 발생:", error);
    alert("서버와의 통신 중 문제가 발생했습니다.");
  } finally {
    isSubmitting = false;
  }
}


// 비밀번호 검증
function validatePassword(currentPassword, newPassword = null, newPasswordConfirm = null) {
  if (!currentPassword) {
    alert("현재 비밀번호를 입력해주세요.");
    return false;
  }
  if (newPassword && newPassword !== newPasswordConfirm) {
    alert("새 비밀번호가 일치하지 않습니다.");
    return false;
  }
  return true;
}


// 알림 테스트 버튼 설정 함수
function setupNotificationTestButtons() {
  setupNotificationTestButton('testTelegramBtn', 'telegram');
  setupNotificationTestButton('testDiscordBtn', 'discord');
}

function setupNotificationTestButton(buttonId, target) {
  const btn = document.getElementById(buttonId);
  if (!btn) return;

  btn.addEventListener('click', async () => {
    const oldText = btn.textContent;

    try {
      btn.disabled = true;
      btn.textContent = '전송 중…';

      const res = await fetch(`/api/test_notification/${target}`, { method: 'GET' });
      const data = await res.json().catch(() => ({}));

      if (res.ok && data?.status === 'success') {
        alert(data.message || '테스트 메시지를 전송했습니다.');
      } else {
        const msg = data?.message || `테스트 실패 (HTTP ${res.status})`;
        alert(msg);
      }
    } catch (err) {
      console.error(`${target} notification test failed:`, err);
      alert('테스트 중 오류가 발생했습니다.');
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }, { once: false });
}


// 쿠키 관리 폼 리스너
function setupCookieFormListeners() {
  const updateCookiesForm = document.getElementById("updateCookiesForm");

  if (updateCookiesForm) {
    updateCookiesForm.addEventListener("submit", async function (event) {
      event.preventDefault();

      // key:value 수집
      const newCookies = {
        chzzk: {
          NID_AUT: "",
          NID_SES: "",
        },
        cime: {
          "mauth-authorization-code": "",
          "session-id": "",
        },
      };

      const inputs = document.querySelectorAll("#updateCookiesForm input");
      inputs.forEach((input) => {
        if (input.type === "submit" || !input.name.trim()) return;

        const name = input.name.trim();
        const value = input.value.trim();

        if (name.includes(".")) {
          const [platform, key] = name.split(".", 2);
          if (!newCookies[platform]) newCookies[platform] = {};
          newCookies[platform][key] = value;
        }
      });

      try {
        const response = await fetch("/cookies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(newCookies),
        });

        if (!response.ok) throw new Error(`HTTP 오류: ${response.status}`);

        const data = await response.json();

        if (data.status === "success") {
          alert("쿠키 업데이트를 완료하였습니다.");
          setTimeout(() => (window.location.href = "/cookies"), 1500);
        } else {
          alert(`쿠키 업데이트에 실패했습니다: ${data.message || "알 수 없는 오류"}`);
        }
      } catch (error) {
        console.error("쿠키 업데이트 중 오류 발생:", error);
        alert("쿠키 업데이트 중 문제가 발생했습니다.");
      }
    });
  }

  const checkChzzkCookieBtn = document.getElementById("checkChzzkCookieBtn");

  if (checkChzzkCookieBtn) {
    checkChzzkCookieBtn.addEventListener("click", async function () {
      const resultEl = document.getElementById("chzzkCookieCheckResult");
      if (resultEl) resultEl.textContent = "치지직 쿠키 확인 중...";

      try {
        const response = await fetch("/api/check_chzzk_cookie", {
          method: "GET",
          cache: "no-store",
        });

        const data = await response.json();

        if (resultEl) {
          resultEl.textContent = data.ok
            ? `정상: ${data.message}`
            : `확인 필요: ${data.message}`;
        }

        alert(data.ok ? `정상: ${data.message}` : `확인 필요: ${data.message}`);
      } catch (error) {
        if (resultEl) resultEl.textContent = `확인 실패: ${error}`;
        alert(`치지직 쿠키 확인 중 오류가 발생했습니다: ${error}`);
      }
    });
  }

  const checkCimeCookieBtn = document.getElementById("checkCimeCookieBtn");

  if (checkCimeCookieBtn) {
    checkCimeCookieBtn.addEventListener("click", async function () {
      const resultEl = document.getElementById("cimeCookieCheckResult");
      if (resultEl) resultEl.textContent = "씨미 쿠키 확인 중...";

      try {
        const response = await fetch("/api/check_cime_cookie", {
          method: "GET",
          cache: "no-store",
        });

        const data = await response.json();

        if (resultEl) {
          resultEl.textContent = data.ok
            ? `정상: ${data.message}`
            : `확인 필요: ${data.message}`;
        }

        alert(data.ok ? `정상: ${data.message}` : `확인 필요: ${data.message}`);
      } catch (error) {
        if (resultEl) resultEl.textContent = `확인 실패: ${error}`;
        alert(`씨미 쿠키 확인 중 오류가 발생했습니다: ${error}`);
      }
    });
  }
}


// 채널 추가 폼에서 플랫폼에 따른 품질/확장자 옵션 구성
function updateQualityAndExtensionOptions() {
  const platform = document.getElementById("platform").value;
  const qualitySelect = document.getElementById("quality");
  const extensionSelect = document.getElementById("extension");

  qualitySelect.innerHTML = "";
  extensionSelect.innerHTML = "";

  if (platform === "chzzk") {
    ["best", "1080p", "720p", "480p", "360p"].forEach((q) => {
      const o = document.createElement("option");
      o.value = q;
      o.textContent = q;
      qualitySelect.appendChild(o);
    });
    [".ts", ".mp4"].forEach((ext) => {
      const o = document.createElement("option");
      o.value = ext;
      o.textContent = ext;
      extensionSelect.appendChild(o);
    });
    extensionSelect.disabled = false;
  } else if (platform === "cime") {
    ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "worst"].forEach((q) => {
      const o = document.createElement("option");
      o.value = q;
      o.textContent = q;
      qualitySelect.appendChild(o);
    });
    const o = document.createElement("option");
    o.value = ".mp4";
    o.textContent = ".mp4";
    extensionSelect.appendChild(o);
    extensionSelect.disabled = true;
  }
}


//채널 관리 페이지: 추가/수정/삭제 리스너
function setupChannelFormListeners() {
  // 채널 추가
  const addChannelForm = document.getElementById("addChannelForm");
  if (addChannelForm) {
    addChannelForm.addEventListener("submit", function (event) {
      event.preventDefault();

      const platform = document.getElementById("platform").value;
      const channelId = document.getElementById("channelId").value;
      const channelName = document.getElementById("channelName").value;
      const outputDir = document.getElementById("outputDir").value;
      const quality = document.getElementById("quality").value;
      const extension = document.getElementById("extension").value;
	  const recordWatchParty = document.getElementById("recordWatchParty").value;
  	  const watchPartyExcludeTags = document.getElementById("watchPartyExcludeTags").value;

	  fetch("/api/channels", {
	    method: "POST",
	    headers: { "Content-Type": "application/json" },
	    body: JSON.stringify({
	  	  platform,
		  id: channelId,
		  name: channelName,
		  output_dir: outputDir,
		  quality,
		  extension,
		  recordWatchParty: recordWatchParty === "true",
		  watchPartyExcludeTags, // 서버가 문자열/배열 모두 정규화
	    }),
	  })
        .then((response) => {
          if (!response.ok) {
            return response.json().then((err) => {
              throw new Error(err.detail);
            });
          }
          return response.json();
        })
        .then((data) => {
          if (data.status === "success") {
            window.location.href = "/channels";
          }
        })
        .catch((error) => {
          console.error("Error adding channel:", error);
          alert("채널 추가 중 오류 발생: " + error.message);
        });
    });
  }


// 같이보기 제외태그 토글 폼
(function setupAddFormWatchPartyToggle() {
  const sel = document.getElementById("recordWatchParty");
  const input = document.getElementById("watchPartyExcludeTags");
  if (!sel || !input) return;
  const apply = () => {
    const on = String(sel.value).toLowerCase() === "true";
    input.disabled = !on; 
  };
  sel.addEventListener("change", apply); // change 이벤트로 충분
  apply(); // 초기상태 반영
})();

// 같이보기 제외태그 토글 수정 카드
(function setupEditCardWatchPartyToggle() {
  const cards = document.querySelectorAll(".channel-edit-form");
  if (!cards.length) return;

  cards.forEach((form) => {
    const sel = form.querySelector(".edit-recordWatchParty");
    const input = form.querySelector(".edit-watchPartyExcludeTags");
    if (!sel || !input) return;
    const apply = () => {
      const on = String(sel.value).toLowerCase() === "true";
      input.disabled = !on;
    };
    sel.addEventListener("change", apply);
    apply(); // 카드별 초기상태 반영
  });
})();


  // 채널 수정(카드 내 인라인 폼)
  document.querySelectorAll(".edit-channel").forEach((button) => {
    button.addEventListener("click", function () {
      const card = this.closest(".channel-card");
      const editForm = card.querySelector(".channel-edit-form");
      const id = this.dataset.id;

      const newPlatform = editForm.querySelector(".edit-platform").value;
      const newName = editForm.querySelector(".edit-channel-name").value;
      const newOutputDir = editForm.querySelector(".edit-output-dir").value;
      const newQuality = editForm.querySelector(".edit-quality").value;
      const newExtension = editForm.querySelector(".edit-extension").value;
	  const newRecordWatchParty = editForm.querySelector(".edit-recordWatchParty").value;
	  const newWatchPartyExcludeTags = editForm.querySelector(".edit-watchPartyExcludeTags")?.value || "";

	  fetch(`/api/channels/${id}`, {
	    method: "PUT",
	    headers: { "Content-Type": "application/json" },
	    body: JSON.stringify({
		  platform: newPlatform,
		  name: newName,
		  output_dir: newOutputDir,
		  quality: newQuality,
		  extension: newExtension,
		  recordWatchParty: newRecordWatchParty === "true",
		  watchPartyExcludeTags: newWatchPartyExcludeTags,
	    }),
	  })
        .then((res) => res.json())
        .then((data) => {
          if (data.status === "success") {
            alert("채널 정보가 저장되었습니다.");
            window.location.reload();
          } else {
            alert("채널 수정 실패: " + data.message);
          }
        })
        .catch((err) => console.error("채널 수정 오류:", err));
    });
  });

  // 수정 취소
  document.querySelectorAll(".cancel-edit").forEach((button) => {
    button.addEventListener("click", function () {
      const editForm = this.closest(".channel-edit-form");
      editForm.style.display = "none";
    });
  });

  // 플랫폼 변경 시 옵션 갱신 및 기존 값 유지
  document.querySelectorAll(".channel-edit-form").forEach((editForm) => {
    updateEditFormOptions(editForm);

    const card = editForm.closest(".channel-card");
    const savedQuality = card.dataset.quality;
    const savedExtension = card.dataset.extension;

    if (savedQuality) editForm.querySelector(".edit-quality").value = savedQuality;
    if (savedExtension) editForm.querySelector(".edit-extension").value = savedExtension;

    const recordWatchElem = editForm.querySelector(".edit-recordWatchParty");
    if (recordWatchElem) recordWatchElem.value = recordWatchElem.value || "false";

    editForm.querySelector(".edit-platform").addEventListener("change", () => {
      updateEditFormOptions(editForm);
    });
  });

  // 채널 삭제
  document.querySelectorAll(".delete-channel").forEach((button) => {
    button.addEventListener("click", function () {
      const id = this.dataset.id;
      deleteChannel(id);
    });
  });
}


// 프롬프트 기반 채널 수정
function editChannel(id, platform, name, outputDir, quality, extension) {
  const platformMap = {
    "치지직": "chzzk",
    "씨미": "cime",
    chzzk: "chzzk",
    cime: "cime",
  };

  const newPlatformInput = prompt("플랫폼을 선택하세요 (치지직, 씨미):", platform);
  if (newPlatformInput === null) return;

  const newPlatformKey = newPlatformInput.trim().toLowerCase();
  const newPlatform = platformMap[newPlatformKey];
  if (!newPlatform) {
    alert("유효한 플랫폼을 입력하세요.");
    return;
  }

  const newName = prompt("새로운 채널명을 입력하세요:", name);
  if (newName === null) return;

  const newOutputDir = prompt("새로운 저장 경로를 입력하세요:", outputDir);
  if (newOutputDir === null) return;

  let qualityOptions = "";
  if (newPlatform === "chzzk") {
    qualityOptions = "best, 1080p, 720p, 480p, 360p";
  } else if (newPlatform === "cime") {
    qualityOptions = "best, 2160p, 1440p, 1080p, 720p, 480p, 360p, worst";
  }

  const newQuality = prompt(`새로운 품질을 입력하세요 (${qualityOptions}):`, quality);
  if (newQuality === null) return;

  let newExtension = "";
  if (newPlatform === "cime") {
    newExtension = ".mp4";
    alert("씨미 플랫폼의 확장자는 .mp4로 고정됩니다.");
  } else {
    newExtension = prompt("새로운 파일 확장자를 입력하세요 (.ts, .mp4):", extension);
    if (newExtension === null) return;
  }

  fetch(`/api/channels/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      platform: newPlatform,
      name: newName,
      output_dir: newOutputDir,
      quality: newQuality,
      extension: newExtension,
    }),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.status === "success") {
        window.location.href = "/channels";
      } else {
        alert("채널 수정에 실패했습니다.");
      }
    })
    .catch((err) => console.error("Error editing channel:", err));
}


// 채널 수정 폼
function updateEditFormOptions(editForm) {
  const platformSelect = editForm.querySelector(".edit-platform");
  const qualitySelect = editForm.querySelector(".edit-quality");
  const extensionSelect = editForm.querySelector(".edit-extension");

  qualitySelect.innerHTML = "";
  extensionSelect.innerHTML = "";

  const platform = platformSelect.value;
  if (platform === "chzzk") {
    ["best", "1080p", "720p", "480p", "360p"].forEach((q) => {
      const o = document.createElement("option");
      o.value = q;
      o.textContent = q;
      qualitySelect.appendChild(o);
    });
    [".ts", ".mp4"].forEach((ext) => {
      const o = document.createElement("option");
      o.value = ext;
      o.textContent = ext;
      extensionSelect.appendChild(o);
    });
    extensionSelect.disabled = false;
  } else if (platform === "cime") {
    ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "worst"].forEach((q) => {
      const o = document.createElement("option");
      o.value = q;
      o.textContent = q;
      qualitySelect.appendChild(o);
    });
    const o = document.createElement("option");
    o.value = ".mp4";
    o.textContent = ".mp4";
    extensionSelect.appendChild(o);
    extensionSelect.disabled = true;
  }
}


// 채널 삭제
function deleteChannel(id) {
  if (!confirm("이 채널을 삭제하시겠습니까?")) return;

  fetch(`/api/channels/${id}`, { method: "DELETE" })
    .then((res) => res.json())
    .then((data) => {
      if (data.status === "success") {
        // 녹화현황 페이지: 해당 카드 제거
        document.querySelector(`.channel[data-channel-id="${cssEsc(id)}"]`)?.remove();
        // 폴링 타이머 중단
        if (pollTimers[id]) {
          clearInterval(pollTimers[id]);
          delete pollTimers[id];
        }
        if (location.pathname === "/channels") location.reload();
      } else {
        alert("채널 삭제에 실패했습니다.");
      }
    })
    .catch((err) => console.error("Error deleting channel:", err));
}


// 파일 관리 크기/퍼센트 포맷
function bytesToHuman(n) {
  if (n == null || isNaN(n)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0,
    v = Number(n);
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

function percent(num, den) {
  if (!den || den <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((num / den) * 100)));
}


// API 래퍼 (same-origin + 오류처리)
async function apiGet(url) {
  const res = await safeFetch(url, { method: "GET" });
  return res.json();
}

async function apiPost(url, body) {
  const res = await safeFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}


// 파일매니저 비활성 모달 표시
function showFMDisabledModal() {
  const app = document.getElementById("files-app");
  if (app) app.style.display = "none";

  const modal = document.getElementById("fm-disabled-modal");
  if (!modal) return;

  modal.classList.remove("hidden");

  // 설정으로 이동
  document.getElementById("go-config")?.addEventListener(
    "click",
    () => { window.location.href = "/config"; },
    { once: true }
  );

  const closeBtn =
    document.getElementById("close-fm-modal") ||
    document.getElementById("close-modal");

  closeBtn?.addEventListener(
    "click",
    () => { window.location.href = "/"; },
    { once: true }
  );
}



//fetch 공통 방어 래퍼
async function safeFetch(url, opts) {
  const res = await fetchWithTimeout(url, { ...(opts || {}) }, 10000);
  if (res.ok) return res;

  const isFilesApi = url.startsWith("/api/files/");
  let detail = "";
  try {
    const j = await res.clone().json();
    detail = j?.detail || "";
  } catch {}

  const fmDisabledByMsg = /file\s*manager.*disabled/i.test(detail);
  const forbiddenByStatus = res.status === 401 || res.status === 403;
  const forbiddenByMsg = /outside.*allowed\s*roots|not\s*allowed|forbidden|permission\s*denied|access\s*denied|restricted/i.test(detail);

  if (isFilesApi && fmDisabledByMsg) {
    showFMDisabledModal();
    const err = new Error("File manager is disabled");
    err.fmDisabled = true;
    err.status = res.status;
    throw err;
  }

  if (isFilesApi && (forbiddenByStatus || forbiddenByMsg)) {
    const err = new Error(detail || "보안 정책상 접근이 제한된 경로입니다.");
    err.status = res.status;
    err.pathDenied = true;
    throw err;
  }

  const msg = detail || `Request failed: ${res.status}`;
  const err = new Error(msg);
  err.status = res.status;
  throw err;
}


// 파일 API 에러 → 사용자 친화 메시지
function formatFileApiError(e, action = "열람", path = "") {
  const t = String(e.message || "");
  const s = e.status || 0;

  if (e.fmDisabled) {
    return "파일 관리 기능이 비활성화되어 있어 작업을 진행할 수 없습니다.\n설정에서 파일관리 기능을 활성화해 주세요.";
  }

  const securityHit =
    e.pathDenied ||
    s === 401 || s === 403 ||
    /outside.*allowed\s*roots|not\s*allowed|forbidden|permission\s*denied|access\s*denied|restricted/i.test(t);

  if (securityHit) {
    return `보안 정책상 접근이 제한된 경로입니다.\n허용된 루트 내부에서만 ${action}할 수 있습니다.` +
           (path ? `\n요청 경로: ${path}` : "");
  }

  if (s === 404 || /not\s*found|no\s*such|invalid\s*path/i.test(t)) {
    return `대상 경로를 찾을 수 없습니다.` + (path ? `\n요청 경로: ${path}` : "");
  }

  if (s >= 500) {
    return `요청 처리 중 서버 오류가 발생했습니다.` +
           (path ? `\n요청 경로: ${path}` : "") +
           `\n허용되지 않은 경로 접근일 가능성이 있습니다.`;
  }

  return `요청 실패: ${t || s || "알 수 없는 오류"}`;
}


// 파일루트/용량 불러와 초기화
async function fetchRootsAndInit() {
  let data;
  try {
    data = await apiGet("/api/files/roots");
  } catch (e) {
    if (e.status === 404 || /not\s*found/i.test(String(e.message || ""))) {
      const ui = await apiGet("/user_info");
      const conf = ui?.config || {};
      const enabled = !!(conf.loginMode && conf.fileManagerEnabled);
      const rootsArr = Array.isArray(conf.fileManagerRoots) ? conf.fileManagerRoots : [];
      const roots = rootsArr.map((p) => ({ path: p, label: p }));

      if (!enabled || roots.length === 0) {
        showFMDisabledModal();
        const err2 = new Error("File manager is disabled or no allowed roots");
        err2.fmDisabled = true;
        throw err2;
      }

      data = { roots, default: roots[0]?.path };
    } else {
      throw e;
    }
  }

  filesRoots = (data.roots || []).map((r) => (typeof r === "string" ? { path: r, label: r } : r));

  const select = document.getElementById("rootSelect");
  select.innerHTML = "";
  filesRoots.forEach((r, idx) => {
    const opt = document.createElement("option");
    opt.value = r.path;
    opt.textContent = r.label || r.path;
    if (idx === 0) opt.selected = true;
    select.appendChild(opt);
  });

  filesCurrentPath = data.default || filesRoots[0]?.path || null;
  if (!filesCurrentPath) {
    alert("허용된 루트가 없습니다. 설정에서 허용 경로를 추가해 주세요.");
    return;
  }
  document.getElementById("pathInput").value = filesCurrentPath;

  try {
    await Promise.all([refreshDiskUsage(), listAndRender(filesCurrentPath)]);
  } catch (e) {
    if (
      /not\s*found|no\s*such|invalid\s*path|not\s*allowed/i.test(String(e.message || "")) &&
      filesRoots[0]?.path &&
      filesRoots[0].path !== filesCurrentPath
    ) {
      await listAndRender(filesRoots[0].path);
      await refreshDiskUsage();
    } else {
      throw e;
    }
  }
}


// 디스크 사용량 갱신
async function refreshDiskUsage() {
  if (!filesRoots.length) return;
  const query = filesRoots.map((r) => `paths=${encodeURIComponent(r.path)}`).join("&");
  const data = await apiGet(`/api/files/disk-usage?${query}`);
  renderDiskCards(data.usages || []);
}


// 디스크 요약(한 줄 칩 형태) 렌더
function renderDiskCards(usages) {
  const wrap = document.getElementById('storage-summary');
  wrap.classList.add('disk-chips');
  wrap.innerHTML = '';

  usages.forEach(u => {
    const used = (u.total || 0) - (u.free || 0);
    const p = percent(used, u.total || 0);

    const chip = document.createElement('div');
    chip.className = 'disk-chip';
	chip.innerHTML = `
	  <span class="disk-ico">💽</span>
	  <strong>${escapeHtml(u.label || u.path)}</strong>
	  <span>${escapeHtml(bytesToHuman(used))} / ${escapeHtml(bytesToHuman(u.total))} (${p}%)</span>
	  <span class="bar"><i style="width:${p}%;"></i></span>
	`;
    wrap.appendChild(chip);
  });
}


// 경로 → 브레드크럼 빌드
function buildBreadcrumbs(pathStr) {
  const bc = document.getElementById("breadcrumbs");
  bc.innerHTML = "";

  const parts = pathStr.split(/[\\/]/).filter(Boolean);
  const seps = pathStr.includes("\\") ? "\\" : "/";

  let accum = "";
  if (pathStr.includes(":")) {
    accum = parts.shift() + seps; // "D:"
  } else if (pathStr.startsWith("/")) {
    accum = seps; // "/"
  }

  const addCrumb = (label, full) => {
    const a = document.createElement("a");
    a.href = "javascript:void(0)";
    a.textContent = label || full || "/";
    a.addEventListener("click", () => changePath(full || seps));
    bc.appendChild(a);
    const sepNode = document.createElement("span");
    sepNode.textContent = " / ";
    bc.appendChild(sepNode);
  };

  if (accum) addCrumb(accum.replace(/[\\\/]$/, ""), accum);

  parts.forEach((p) => {
    accum = accum ? (accum.endsWith(seps) ? accum + p : accum + seps + p) : p;
    addCrumb(p, accum);
  });

  if (bc.lastChild) bc.removeChild(bc.lastChild);
}


// 디렉터리 목록 로드 + 렌더
async function listAndRender(pathStr) {
  try {
    const data = await apiGet(`/api/files/ls?path=${encodeURIComponent(pathStr)}`);
    filesCurrentPath = data.path || pathStr;
    const raw = data.items || [];
	filesAllItems = raw.filter(it => !isHiddenItem(it));

    document.getElementById('pathInput').value = filesCurrentPath;
    buildBreadcrumbs(filesCurrentPath);
    renderFileTable(filesAllItems);

    // 경로 바뀔 때 모바일 선택 초기화
    clearSelection();
  } catch (e) { throw e; }
}


// 수정일 포맷
function fmtMtime(m) {
  if (m == null) return '-';
  if (typeof m === 'number') {
    const d = new Date(m * 1000);
    return isNaN(d) ? '-' : d.toLocaleString();
  }
  return String(m);
}


// 파일/폴더 리스트형 렌더
function renderFileTable(items) {
  // 모바일이면 테이블 대신 모바일 렌더러
  if (window.matchMedia('(max-width: 900px)').matches) {
    renderFileListMobile(items);
    return;
  }

  const tbody = document.getElementById('fileTableBody');
  const filterText = (document.getElementById('fileSearch').value || '').toLowerCase();
  tbody.innerHTML = '';

  const filtered = items.filter(it => !filterText || (it.name || '').toLowerCase().includes(filterText));

  if (!filtered.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6; // 체크박스 열 추가로 6열
    td.textContent = '표시할 항목이 없습니다.';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  // 헤더의 전체선택 체크박스(#chkAll)와 동기화
  const chkAll = document.getElementById('chkAll');
  if (chkAll) {
    // 현재 화면에 보이는 항목 기준으로 체크 상태 반영
    const totalCount    = filtered.length;
    const selectedCount = filtered.filter(it => selectedPaths.has(it.path)).length;
    chkAll.indeterminate = selectedCount > 0 && selectedCount < totalCount;
    chkAll.checked       = totalCount > 0 && selectedCount === totalCount;

    chkAll.onchange = () => {
      filtered.forEach(it => {
        // 폴더도 선택 허용(필요시 제한하려면 it.is_dir은 건너뛰기)
        if (chkAll.checked) selectedPaths.add(it.path);
        else selectedPaths.delete(it.path);
      });
      // 다시 렌더해 UI 반영
      renderFileTable(items);
    };
  }

  filtered.forEach(it => {
    const tr = document.createElement('tr');

	// 선택 체크박스
	const tdCheck = document.createElement('td');
	tdCheck.className = 'col-check';
	const cb = document.createElement('input');
	cb.type = 'checkbox';
	cb.className = 'row-check';
	cb.value = it.path;
	cb.checked = selectedPaths.has(it.path);
	cb.addEventListener('change', () => {
	  if (cb.checked) selectedPaths.add(it.path);
	  else selectedPaths.delete(it.path);

	  // 헤더 전체선택 상태 즉시 갱신
	  const allRowChecks = tbody.querySelectorAll('input.row-check');
	  const total = allRowChecks.length;
	  const sel   = Array.from(allRowChecks).filter(x => x.checked).length;
	  const hdr   = document.getElementById('chkAll');
	  if (hdr) {
		hdr.indeterminate = sel > 0 && sel < total;
		hdr.checked       = sel > 0 && sel === total;
	  }

	  // 모바일 액션바 쓰면 상태 갱신
	  if (typeof updateMobileActionBar === 'function') updateMobileActionBar();
	});
	tdCheck.appendChild(cb);


    // 유형
    const tdType = document.createElement('td');
    tdType.className = 'col-type';
    tdType.textContent = it.is_dir ? '폴더' : (it.ext || '파일');

    // 이름
    const tdName = document.createElement('td');
    tdName.className = 'col-name';
    const nameBtn = document.createElement('button');
    nameBtn.className = 'linklike';
    nameBtn.textContent = it.name || '(이름없음)';
    if (it.is_dir) {
      nameBtn.title = '이 폴더 열기';
      nameBtn.addEventListener('click', () => changePath(it.path));
    } else {
      nameBtn.title = '미리보기 (미디어 파일)';
      nameBtn.addEventListener('click', () => openPreview(it.path));
    }
    tdName.appendChild(nameBtn);

    // 크기
    const tdSize = document.createElement('td');
    tdSize.className = 'col-size';
    tdSize.textContent = it.is_dir ? '-' : bytesToHuman(it.size);

    // 수정일
    const tdMtime = document.createElement('td');
    tdMtime.className = 'col-mtime';
    tdMtime.textContent = it.mtime || '-';

    // 작업(1×4)
    const tdAct = document.createElement('td');
    tdAct.className = 'actions';

    const btnMove   = document.createElement('button');  btnMove.textContent   = '이동';   btnMove.disabled   = !!it.locked;
    const btnDelete = document.createElement('button');  btnDelete.textContent = it.locked ? '잠김' : '삭제'; btnDelete.disabled = !!it.locked;
    const btnRename = document.createElement('button');  btnRename.textContent = '수정';   btnRename.disabled = !!it.locked;
    const btnDetail = document.createElement('button');  btnDetail.textContent = '상세';

    btnRename.addEventListener('click', () => onRename(it));
    btnMove  .addEventListener('click', () => onMove(it));
    btnDelete.addEventListener('click', () => onDelete(it));
    btnDetail.addEventListener('click', () => {
      const lines = [
        `이름 : ${it.name}`,
        `종류 : ${it.is_dir ? '폴더' : (it.ext || '파일')}`,
        `크기 : ${it.is_dir ? '-' : bytesToHuman(it.size)}`,
        `수정 : ${it.mtime || '-'}`,
        `경로 : ${it.path}`
      ];
      alert(lines.join('\n'));
    });

    [btnMove, btnDelete, btnRename, btnDetail].forEach(b => tdAct.appendChild(b));

    // 행 조립 (체크박스 열이 맨 앞)
    tr.appendChild(tdCheck);
    tr.appendChild(tdType);
    tr.appendChild(tdName);
    tr.appendChild(tdSize);
    tr.appendChild(tdMtime);
    tr.appendChild(tdAct);
    tbody.appendChild(tr);
  });
}


// 경로 변경
async function changePath(nextPath) {
  try {
    await listAndRender(nextPath);
  } catch (e) {
    alert(formatFileApiError(e, "열람", nextPath));  
  }
}


// 상세 모달: 크기/수정일/전체 경로 등 표시
function showDetailModal(it){
  const modal = document.getElementById('file-detail-modal');
  const body  = document.getElementById('detail-body');
  if (!modal || !body) return;

  const isDir = !!it.is_dir;
  const iconEmoji = isDir ? '📁' : '📄';
  const size = isDir ? '-' : bytesToHuman(it.size);
  const mtime = it.mtime || '-';

  body.innerHTML = `
    <div class="detail-icon">${escapeHtml(iconEmoji)}</div>
    <div class="detail-grid">
      <div class="detail-row"><b>이름</b> <span>${escapeHtml(it.name || '')}</span></div>
      <div class="detail-row"><b>유형</b> <span>${escapeHtml(isDir ? '폴더' : (it.ext || '파일'))}</span></div>
      <div class="detail-row"><b>크기</b> <span>${escapeHtml(size)}</span></div>
      <div class="detail-row"><b>수정일</b> <span>${escapeHtml(fmtMtime(it.mtime))}</span></div>
      <div class="detail-row"><b>경로</b> <span style="word-break:break-all">${escapeHtml(it.path || '')}</span></div>
    </div>
  `;

  modal.classList.remove('hidden');
  document.getElementById('detail-close')?.addEventListener('click', () => {
    modal.classList.add('hidden');
  }, { once:true });

  // 바깥 클릭 닫기
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.classList.add('hidden');
  }, { once:true });
}


// 파일 다운로드/미리보기
function openPreview(fullPath) {
  const a = document.createElement("a");
  a.href = `/api/files/download?path=${encodeURIComponent(fullPath)}`;
  a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}


// 파일 이름 변경
async function onRename(item) {
  const newName = prompt("새 이름을 입력하세요", item.name);
  if (!newName || newName === item.name) return;
  try {
    await apiPost("/api/files/rename", { path: item.path, new_name: newName });
    await listAndRender(filesCurrentPath);
  } catch (e) {
    alert(`이름 변경 실패: ${e.message}`);
  }
}


// 파일 삭제(완전삭제/휴지통)
async function onDelete(item) {
  const hard = confirm("완전 삭제하시겠습니까?\n(확인=완전삭제 / 취소=휴지통으로 이동)");
  const msg = hard ? "완전 삭제" : "휴지통 이동";
  if (!confirm(`${item.name}\n정말 ${msg} 하시겠습니까?`)) return;

  try {
    await apiPost("/api/files/delete", { paths: [item.path], hard: !!hard });
    await listAndRender(filesCurrentPath);
  } catch (e) {
    alert(`${msg} 실패: ${e.message}`);
  }
}


// 스트림복사 공통 핸들러
async function onStreamCopySelected() {
  const items = getSelectedItems().filter(it => !it.is_dir);
  if (!items.length) { alert("스트림복사할 파일을 선택하세요."); return; }

  try {
    const res = await apiPost('/api/files/streamcopy', { paths: items.map(i => i.path) });
    const failed = (res.results || []).filter(r => !r.ok);
    if (failed.length) {
      const msg = failed.map(f => `- ${f.src}\n  → ${f.error || '실패'}`).join('\n');
      alert(`일부 실패:\n${msg}`);
    } else {
      alert('스트림복사가 완료되었습니다.');
    }
    // 복사본도 보이도록 새로고침
    await listAndRender(filesCurrentPath);
  } catch (e) {
    alert(formatFileApiError(e, "스트림복사"));
  }
}

// 파일매니저 페이지 초기 세팅
function setupFilesPage() {
  const ml = document.getElementById('fileListMobile');
  if (ml) {
    ml.style.webkitTouchCallout = 'none';
    ml.style.webkitUserSelect = 'none';
    ml.style.userSelect = 'none';
  }

  // 루트 변경
  document.getElementById("rootSelect")?.addEventListener("change", async (e) => {
    const p = e.target.value;
    if (p) await changePath(p);
    await refreshDiskUsage();
  });

  // 상위 폴더
  document.getElementById("btnUp")?.addEventListener("click", async () => {
    const sep = (filesCurrentPath || "").includes("\\") ? "\\" : "/";
    let p = filesCurrentPath || "";
    if (!p) return;

    p = p.replace(/[\\\/]+$/, ""); // 끝 구분자 제거
    const parts = p.split(/[\\/]/).filter(Boolean);

    if (p.includes(":")) {
      // Windows
      if (parts.length > 1) {
        parts.pop();
        p = parts[0] + sep + parts.slice(1).join(sep);
      } else {
        p = parts[0] + sep; // D:\
      }
    } else if (p.startsWith("/")) {
      // Unix
      if (parts.length > 0) parts.pop();
      p = "/" + parts.join(sep);
      if (p === "") p = "/";
    }
    await changePath(p);
  });

  // 새로고침
  document.getElementById("btnRefresh")?.addEventListener("click", async () => {
    await Promise.all([refreshDiskUsage(), listAndRender(filesCurrentPath)]);
  });

  // 새 폴더
  document.getElementById("btnMkdir")?.addEventListener("click", async () => {
    const name = prompt("새 폴더 이름을 입력하세요", "새 폴더");
    if (!name) return;
    try {
      await apiPost("/api/files/mkdir", { path: filesCurrentPath, name });
      await listAndRender(filesCurrentPath);
    } catch (e) {
      alert(`폴더 생성 실패: ${e.message}`);
    }
  });

  // 경로 입력 후 Enter 이동
  document.getElementById("pathInput")?.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      const p = e.target.value.trim();
      if (p) await changePath(p);
    }
  });

  // 클라이언트 사이드 필터
  document.getElementById("fileSearch")?.addEventListener("input", () => {
    renderFileTable(filesAllItems);
  });

  // 모바일 액션바 바인딩 (setupFilesPage() 안쪽)
  function setupMobileActionBar() {
    // 이동/삭제/수정/상세
    document.getElementById('mobMove')  ?.addEventListener('click', onMoveSelected);
    document.getElementById('mobDelete')?.addEventListener('click', onDeleteSelected);
    document.getElementById('mobRename')?.addEventListener('click', onRenameSelected);
    document.getElementById('mobDetail')?.addEventListener('click', onDetailSelected);

    // 선택모드 진입
    document.getElementById('mobSelect')?.addEventListener('click', () => {
  	  enterSelectMode();        // 선택모드 on
	  updateMobileActionBar();  // 버튼 상태 즉시 반영
    });

    // 취소 버튼: 선택 해제 + 선택모드 종료
    document.getElementById('mobCancel')?.addEventListener('click', () => {
 	  clearSelection();                     // 선택 모두 해제
	  selectMode = false;                   // 선택모드 종료
	  document.body.classList.remove('select-mode');
	  updateMobileActionBar();              // 버튼 상태 반영
    });
}

  setupMobileActionBar();
  updateMobileActionBar(); // 초기 상태 반영(취소 버튼 숨김 등)


  // 스트림복사 버튼 바인딩
  (function bindStreamCopyButton() {
    const btn = document.getElementById('btnStreamCopy');
    if (!btn || btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';

    let scLock = false;

    const handler = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (scLock) return;
      scLock = true;
      btn.disabled = true;

      try {
        await onStreamCopySelected();
      } finally {
        btn.disabled = false;
        scLock = false;
      }
    };

    if (window.PointerEvent) {
      btn.addEventListener('pointerup', handler, { passive: false });

      btn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') handler(e);
      });
    } else if ('ontouchend' in window) {
      btn.addEventListener('touchend', handler, { passive: false });
    } else {
      btn.addEventListener('click', handler, { passive: false });
    }
  })();

  // 초기화
  fetchRootsAndInit().catch((err) => {
    console.error("파일관리 초기화 실패:", err);
    if (!err?.fmDisabled) {
      alert("파일관리 초기화 실패: " + (err?.message || "Unknown error"));
    }
  });
}


// 일매니저 관련 UI 토글/행 추가
function setupConfigFileManagerUI() {
  const modeSel = document.getElementById("fileManagerMode");
  const section = document.getElementById("fm-roots-section");
  if (!modeSel || !section) return;

  const toggle = () => {
    const v = modeSel.value;
    section.style.display = v === "whitelist" ? "block" : "none";
  };
  modeSel.addEventListener("change", toggle);
  toggle();

  const list = document.getElementById("fm-roots-list");
  document.getElementById("add-fm-root")?.addEventListener("click", () => {
    const row = document.createElement("div");
    row.className = "fm-root-row";
    row.style.cssText = "display:flex;gap:8px;align-items:center;margin:6px 0;";
    row.innerHTML = `
      <input type="text" name="fileManagerRoots" placeholder="예: C:/recordWEB/record" style="flex:1;">
      <button type="button" class="rm-root">삭제</button>`;
    list.appendChild(row);
  });

  list?.addEventListener("click", (e) => {
    const btn = e.target.closest(".rm-root");
    if (!btn) return;
    btn.closest(".fm-root-row")?.remove();
  });
}


// 모바일 목록 렌더러(버튼 없음, 선택용 체크박스 + 썸/이름)
function renderFileListMobile(items) {
  const wrap = document.getElementById('fileListMobile');
  const filterText = (document.getElementById('fileSearch')?.value || '').toLowerCase();
  wrap.innerHTML = '';

  const filtered = items.filter(it => !filterText || (it.name || '').toLowerCase().includes(filterText));
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.textContent = '표시할 항목이 없습니다.';
    empty.style.cssText = 'padding:10px;color:#666;';
    wrap.appendChild(empty);
    updateMobileActionBar();
    return;
  }

  filtered.forEach(it => {
    const row = document.createElement('div');
    row.className = 'fm-item ' + (it.is_dir ? 'kind-folder' : 'kind-file');
    row.dataset.path = it.path;

    // 체크박스(선택모드에서만 CSS로 보임)
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'fm-select';
    cb.checked = selectedPaths.has(it.path);
    cb.addEventListener('change', () => {
      if (cb.checked) enterSelectMode();
      toggleSelect(it.path, row, cb.checked);
      exitSelectModeIfNone();
      updateMobileActionBar();
    });
    row.appendChild(cb);

    // 썸
    const thumb = document.createElement('div');
    thumb.className = 'fm-thumb';
    row.appendChild(thumb);

    // 텍스트 묶음
    const textBox = document.createElement('div');
    textBox.className = 'fm-text';

    const nameEl = document.createElement('div');
    nameEl.className = 'fm-name';
    nameEl.title = it.name || '';
    nameEl.textContent = it.name || '(이름없음)';

    const metaEl = document.createElement('div');
    metaEl.className = 'fm-meta';
    metaEl.textContent = it.is_dir ? '폴더' : (it.ext || '파일');

    textBox.appendChild(nameEl);
    textBox.appendChild(metaEl);
    row.appendChild(textBox);

    // 길게 눌러 선택모드 / 짧게 탭은 열기
    let pressTimer = null;
    let longPressed = false;
    const clearTimer = () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } };

    row.addEventListener('pointerdown', () => {
      longPressed = false;
      pressTimer = setTimeout(() => {
        longPressed = true;
        enterSelectMode();
        toggleSelect(it.path, row, true);
        const c = row.querySelector('.fm-select'); if (c) c.checked = true;
        updateMobileActionBar();
      }, LONG_PRESS_MS);
    });

    row.addEventListener('pointerup', () => {
      clearTimer();
      if (longPressed) return;
      if (!selectMode) {
        if (it.is_dir) changePath(it.path);
        else openPreview(it.path);
      } else {
        const nowChecked = !selectedPaths.has(it.path);
        toggleSelect(it.path, row, nowChecked);
        const c = row.querySelector('.fm-select'); if (c) c.checked = nowChecked;
        exitSelectModeIfNone();
        updateMobileActionBar();
      }
    });

    row.addEventListener('pointerleave', clearTimer);
    row.addEventListener('pointercancel', clearTimer);

    if (it.locked) {
      const lock = document.createElement('div');
      lock.className = 'fm-meta';
      lock.textContent = '잠김(녹화중)';
      textBox.appendChild(lock);
    }

    if (selectedPaths.has(it.path)) row.classList.add('is-selected');
    wrap.appendChild(row);
  });

  updateMobileActionBar();
}


// 선택 상태 초기화
function clearSelection() {
  // 내부 상태 비우기
  selectedPaths.clear();

  // (데스크탑) 행 체크박스 전부 원복
  document.querySelectorAll('input.row-check').forEach(cb => { cb.checked = false; });

  // (데스크탑) 헤더 전체선택도 해제
  const hdr = document.getElementById('chkAll');
  if (hdr) { hdr.checked = false; hdr.indeterminate = false; }

  // (모바일) UI 원복
  document.querySelectorAll('#fileListMobile .fm-item').forEach(el => {
    el.classList.remove('is-selected');
    const cb = el.querySelector('.fm-select');
    if (cb) cb.checked = false;
  });

  // 선택모드 종료
  selectMode = false;
  document.body.classList.remove('select-mode');

  // 스트림복사 버튼 상태 원복
  const scBtn = document.getElementById('btnStreamCopy');
  if (scBtn) {
    scBtn.disabled = false;
    scBtn.classList.remove('is-busy');   // 진행중 클래스 쓰는 경우
    // scBtn.textContent = '스트림복사'; // 텍스트를 바꿨다면 주석 해제
  }

  // 액션바 버튼 상태 갱신
  updateMobileActionBar();
}


// 현재 선택된 항목들 반환 
function getSelectedItems() {
  const map = new Map(filesAllItems.map(it => [it.path, it]));

  // PC(테이블)에서 직접 체크한 값
  const fromPc = Array.from(document.querySelectorAll('input.row-check:checked'))
    .map(cb => cb.value);

  // 모바일 선택모드(길게 눌러서 담긴 Set)
  const fromMobile = Array.from(selectedPaths);

  // 합집합 → filesAllItems에 실제 있는 항목만 반환
  const uniq = Array.from(new Set([...fromPc, ...fromMobile]));
  return uniq.map(p => map.get(p)).filter(Boolean);
}


// 버튼 활성/비활성 업데이트 (Select/Cancel 교대 표시 포함)
function updateMobileActionBar() {
  const cnt = selectedPaths.size;
  const any = cnt > 0;
  const one = cnt === 1;

  const $mv  = document.getElementById('mobMove');
  const $del = document.getElementById('mobDelete');
  const $ren = document.getElementById('mobRename');
  const $det = document.getElementById('mobDetail');
  const $can = document.getElementById('mobCancel');
  const $sel = document.getElementById('mobSelect');

  // 개수에 따라 enable/disable
  [$mv, $del].forEach(b => b && (b.disabled = !any));
  [$ren, $det].forEach(b => b && (b.disabled = !one));

  if ($can) { $can.classList.toggle('hidden', !any); $can.disabled = !any; }
  if ($sel) { $sel.classList.toggle('hidden',  any); $sel.disabled =  any; }
}


// 폴더 선택(이동) 모달
async function openMovePicker(initialPath, onChoose) {
  const modal    = document.getElementById('move-picker-modal');
  const rootSel  = document.getElementById('mp-rootSelect');
  const listBox  = document.getElementById('mp-list');
  const crumbsEl = document.getElementById('mp-breadcrumbs');
  const btnOk    = document.getElementById('mp-choose');
  const btnCancel= document.getElementById('mp-cancel');

  modal.classList.remove('hidden');
  listBox.innerHTML = '<div style="padding:10px;">불러오는 중…</div>';

  try {
    if (!rootSel.dataset.filled) {
      let rootsArr = [];
      if (Array.isArray(filesRoots) && filesRoots.length) {
        rootsArr = filesRoots;
      } else {
        const rootsData = await apiGet('/api/files/roots');
        rootsArr = rootsData.roots || [];
      }
      rootSel.innerHTML = '';
      rootsArr.forEach(r => {
        const path = typeof r === 'string' ? r : (r.path || '');
        const label = typeof r === 'string' ? r : (r.label || path);
        if (!path) return;
        const o = document.createElement('option');
        o.value = path; o.textContent = label;
        rootSel.appendChild(o);
      });
      rootSel.dataset.filled = '1';
    }
  } catch (e) {
    // 루트 불러오기 실패 → 현재 경로로라도 동작
    rootSel.innerHTML = '';
    const o = document.createElement('option');
    o.value = filesCurrentPath || '/';
    o.textContent = o.value;
    rootSel.appendChild(o);
    rootSel.dataset.filled = '1';
  }

  // 3) 시작 경로 설정
  movePickerPath = initialPath || rootSel.value || filesCurrentPath || '/';

  rootSel.onchange = async () => {
    movePickerPath = rootSel.value;
    await renderMovePickerList(movePickerPath, listBox, crumbsEl);
  };

  // 4) 폴더 목록 렌더 (실패해도 안내 문구 표시)
  try {
    await renderMovePickerList(movePickerPath, listBox, crumbsEl);

  } catch (e) {
    const msg = formatFileApiError(e, "탐색", movePickerPath).replace(/\n/g, "<br>");
    listBox.innerHTML = `<div style="padding:10px;color:#b91c1c;">${msg}</div>`;
  }

  // 5) OK/취소
  const close = () => {
    modal.classList.add('hidden');
    btnOk.onclick = btnCancel.onclick = rootSel.onchange = null;
  };
  btnCancel.onclick = close;
  btnOk.onclick = async () => {
    try { await onChoose(movePickerPath); } finally { close(); }
  };

  // 바깥 클릭 닫기
  const outsideHandler = (e) => { if (e.target === modal) close(); };
  modal.addEventListener('click', outsideHandler, { once:true });
}


async function renderMovePickerList(path, box, crumbsEl) {
  try {
    const data = await apiGet(`/api/files/ls?path=${encodeURIComponent(path)}`);
    movePickerPath = data.path || path;

    // 브레드크럼 다시 그림
    mpBuildBreadcrumbs(movePickerPath, crumbsEl);

    // 디렉터리만 필터
    const dirs = (data.items || []).filter(it => it.is_dir);

    box.innerHTML = '';
    if (!dirs.length) {
      const empty = document.createElement('div');
      empty.textContent = '하위 폴더가 없습니다.';
      empty.style.cssText = 'padding:10px;color:#666;';
      box.appendChild(empty);
      return;
    }

    dirs.forEach(d => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;';
      row.innerHTML = `<span>📁</span><div style="flex:1;min-width:0;">${escapeHtml(d.name)}</div>`;
      row.addEventListener('click', async () => {
        await renderMovePickerList(d.path, box, crumbsEl);
      });
      box.appendChild(row);
    });
  } catch (e) {
    const msg = formatFileApiError(e, "탐색", path).replace(/\n/g, "<br>");
    box.innerHTML = `<div style="padding:10px;color:#b91c1c;">${msg}</div>`;
  }
}

function mpBuildBreadcrumbs(fullPath, el) {
  el.innerHTML = '';
  const sep = fullPath.includes('\\') ? '\\' : '/';
  const parts = fullPath.split(/[\\/]/).filter(Boolean);

  let accum = '';
  if (fullPath.includes(':')) { accum = parts.shift() + sep; }
  else if (fullPath.startsWith('/')) { accum = sep; }

  const add = (label, target) => {
    const a = document.createElement('a');
    a.href = 'javascript:void(0)';
    a.textContent = label || target || '/';
    a.addEventListener('click', async () => {
      await renderMovePickerList(target || sep, document.getElementById('mp-list'), el);
    });
    el.appendChild(a);
    const s = document.createElement('span'); s.textContent = ' / '; el.appendChild(s);
  };

  if (accum) add(accum.replace(/[\\\/]$/, ''), accum);

  parts.forEach(p => {
    accum = accum ? (accum.endsWith(sep) ? accum + p : accum + sep + p) : p;
    add(p, accum);
  });

  if (el.lastChild) el.removeChild(el.lastChild);
}


async function onMoveSelected() {
  const items = getSelectedItems();
  if (!items.length) return;
  try {
    await openMovePicker(filesCurrentPath, async (dst) => {
      await apiPost('/api/files/move', { srcs: items.map(i => i.path), dst_dir: dst });
      await listAndRender(filesCurrentPath);
      clearSelection();
    });
  } catch (e) {
    alert('이동 창을 여는 데 실패했습니다: ' + (e.message || e));
  }
}


async function onMove(item) {
  try {
    await openMovePicker(filesCurrentPath, async (dst) => {
      await apiPost('/api/files/move', { srcs: [item.path], dst_dir: dst });
      await listAndRender(filesCurrentPath);
    });
  } catch (e) {
    alert('이동 창을 여는 데 실패했습니다: ' + (e.message || e));
  }
}


async function onDeleteSelected() {
  const items = getSelectedItems();
  if (!items.length) return;
  const hard = confirm('완전 삭제하시겠습니까?\n(확인=완전삭제 / 취소=휴지통)');
  if (!confirm(`${items.length}개 항목을 ${hard?'완전 삭제':'휴지통 이동'} 하시겠습니까?`)) return;
  try {
    await apiPost('/api/files/delete', { paths: items.map(i => i.path), hard: !!hard });
    await listAndRender(filesCurrentPath);
    clearSelection();
  } catch (e) { alert('삭제 실패: ' + e.message); }
}


async function onRenameSelected() {
  const items = getSelectedItems();
  if (items.length !== 1) return;
  const it = items[0];
  const newName = prompt('새 이름을 입력하세요', it.name);
  if (!newName || newName === it.name) return;
  try {
    await apiPost('/api/files/rename', { path: it.path, new_name: newName });
    await listAndRender(filesCurrentPath);
    clearSelection();
  } catch (e) { alert('이름 변경 실패: ' + e.message); }
}


function onDetailSelected() {
  const items = getSelectedItems();
  if (items.length !== 1) return;
  const it = items[0];
  const lines = [
    `이름 : ${it.name}`,
    `종류 : ${it.is_dir ? '폴더' : (it.ext || '파일')}`,
    `크기 : ${it.is_dir ? '-' : bytesToHuman(it.size)}`,
    `수정 : ${it.mtime || '-'}`,
    `경로 : ${it.path}`
  ];
  alert(lines.join('\n'));
}


// 선택모드 진입/해제
function enterSelectMode() {
  if (selectMode) return;
  selectMode = true;
  document.body.classList.add('select-mode');

  // ↑ 선택모드에서도 툴바 클릭 보장
  const tb = document.querySelector('.file-toolbar');
  if (tb) {
    tb.style.pointerEvents = 'auto';
    tb.style.position = 'relative';
    tb.style.zIndex = '1001';
  }

  updateMobileActionBar();
}


function exitSelectModeIfNone() {
  if (!selectMode) return;
  if (selectedPaths.size === 0) {
    selectMode = false;
    document.body.classList.remove('select-mode');

    // 원복
    const tb = document.querySelector('.file-toolbar');
    if (tb) {
      tb.style.pointerEvents = '';
      tb.style.position = '';
      tb.style.zIndex = '';
    }

    updateMobileActionBar();
  }
}


// 선택 토글
function toggleSelect(path, card, forceChecked = null) {
  const wantCheck = forceChecked === null ? !selectedPaths.has(path) : !!forceChecked;
  if (wantCheck) {
    selectedPaths.add(path);
    card?.classList.add('is-selected');
  } else {
    selectedPaths.delete(path);
    card?.classList.remove('is-selected');
  }
}
