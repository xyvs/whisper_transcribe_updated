(function () {
  'use strict';

  // Try both common plugin IDs:
  // - folder name ("whisper_transcribe")
  // - YAML name ("WhisperTranscribe")
  const PLUGIN_IDS = ['whisper_transcribe', 'WhisperTranscribe'];
  const MENU_ITEM_ID = 'whisper-transcribe-menu-item';
  // The three‑dot "operations menu" (ID: operation-menu) that contains actions like rescan, generate, etc.
  const OPERATIONS_TOGGLE_ID = 'operation-menu';

  function getSceneIdFromURL() {
    try {
      // Try pathname first: /scenes/123
      const pathMatch = window.location.pathname.match(/\/scenes\/(\d+)/);
      if (pathMatch) return parseInt(pathMatch[1], 10);

      // Fallback to hash routes: #/scenes/123
      const hashMatch = window.location.hash.match(/\/scenes\/(\d+)/);
      if (hashMatch) return parseInt(hashMatch[1], 10);
    } catch (e) {
      console.warn('[WhisperTranscribe] Failed to parse scene id from URL:', e);
    }
    return undefined;
  }

  async function resolvePluginId(graphqlURL) {
    const query = `query { plugins { id name } }`;
    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const json = await res.json();
      if (json.errors || !json.data || !json.data.plugins) return null;

      const plugins = json.data.plugins;

      // Prefer exact id matches first
      for (const p of plugins) {
        if (PLUGIN_IDS.includes(p.id)) return p.id;
      }
      // Then match by name
      for (const p of plugins) {
        if (PLUGIN_IDS.includes(p.name)) return p.id;
      }
      // Heuristic fallback: anything containing "whisper"
      for (const p of plugins) {
        const n = (p.name || '').toLowerCase();
        const i = (p.id || '').toLowerCase();
        if (n.includes('whisper') || i.includes('whisper')) return p.id;
      }
      return null;
    } catch (e) {
      console.error('[WhisperTranscribe] Failed to resolve plugin id:', e);
      return null;
    }
  }

  function basename(path) {
    if (typeof path !== 'string') return undefined;
    const trimmed = path.trim();
    if (!trimmed) return undefined;
    const parts = trimmed.split(/[\\/]/);
    return parts[parts.length - 1] || undefined;
  }

  async function buildJobDescription(graphqlURL, sceneId) {
    const query = `
      query WhisperTranscribeScene($id: ID!) {
        findScene(id: $id) {
          title
          files {
            path
          }
        }
      }
    `;

    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, variables: { id: sceneId } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);

      const json = await res.json();
      if (json.errors || !json.data || !json.data.findScene) return `whisper_transcribe: scene ${sceneId}`;

      const scene = json.data.findScene;
      const filePath = scene.files?.[0]?.path;
      const fileLabel = basename(filePath);
      if (fileLabel) return `whisper_transcribe: ${fileLabel}`;

      const title = (scene.title || '').trim();
      if (title) return `whisper_transcribe: ${title}`;

      return `whisper_transcribe: scene ${sceneId}`;
    } catch (e) {
      console.warn('[WhisperTranscribe] Failed to build job description:', e);
      return `whisper_transcribe: scene ${sceneId}`;
    }
  }

  async function runTranscribe(sceneId) {
    const mutation = `
      mutation RunPluginTask($plugin_id: ID!, $args_map: Map!, $description: String) {
        runPluginTask(plugin_id: $plugin_id, args_map: $args_map, description: $description)
      }
    `;
    const args_map = { mode: 'transcribe_scene_task', scene_id: sceneId };
    const base = document.querySelector('base')?.getAttribute('href') || '/';
    const graphqlURL = new URL('graphql', new URL(base, window.location.href)).toString();

    // Resolve plugin id; if not found, abort to avoid server-side panic on unknown id.
    const resolvedId = await resolvePluginId(graphqlURL);
    if (!resolvedId) {
      console.error('[WhisperTranscribe] Could not resolve plugin id. Aborting to avoid server error.');
      alert('Whisper Transcribe plugin not found on server. Try reloading plugins and refreshing the page.');
      return;
    }

    const description = await buildJobDescription(graphqlURL, sceneId);

    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: mutation, variables: { plugin_id: resolvedId, args_map, description } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const json = await res.json();
      if (json.errors) {
        console.error('[WhisperTranscribe] GraphQL errors:', json.errors);
        alert('Failed to start transcription. See console for details.');
        return;
      }
      console.debug('[WhisperTranscribe] Transcription queued as job:', json.data?.runPluginTask);
    } catch (e) {
      console.error('[WhisperTranscribe] Request failed:', e);
      alert('Failed to start transcription. See console for details.');
    }
  }

  function closeDropdown(menuEl) {
    const dropdown = menuEl?.closest('.dropdown');
    menuEl?.classList.remove('show');
    dropdown?.classList.remove('show');
  }

  function createMenuItem(menuEl) {
    if (!menuEl) return;
    const existing = document.getElementById(MENU_ITEM_ID);
    if (existing) {
      // If it's already in the correct menu, nothing to do.
      if (menuEl.contains(existing)) return;
      existing.remove();
    }

    const item = document.createElement('button');
    item.id = MENU_ITEM_ID;
    item.type = 'button';
    item.className = 'dropdown-item bg-secondary text-white';
    item.textContent = 'Transcribe scene (Whisper)';
    item.addEventListener('click', function (ev) {
      ev.preventDefault();
      const sceneId = getSceneIdFromURL();
      if (!sceneId) {
        alert('Whisper Transcribe: could not determine scene id from URL.');
        return;
      }
      runTranscribe(sceneId);
      closeDropdown(menuEl);
    });

    // Try to position after "Generate default thumbnail"
    const items = Array.from(menuEl.querySelectorAll('.dropdown-item'));
    const defaultThumbItem = items.find((el) => {
      const text = (el.textContent || '').trim().toLowerCase();
      return text.includes('generate default thumbnail');
    });
    if (defaultThumbItem?.parentElement === menuEl) {
      defaultThumbItem.insertAdjacentElement('afterend', item);
    } else {
      // Fall back: place before delete to keep destructive actions at the end.
      const deleteItem = items.find((el) => {
        const text = (el.textContent || '').trim().toLowerCase();
        return text.includes('delete');
      });
      if (deleteItem?.parentElement === menuEl) {
        menuEl.insertBefore(item, deleteItem);
      } else {
        menuEl.appendChild(item);
      }
    }
  }

  function findOperationsMenu() {
    const toggle = document.getElementById(OPERATIONS_TOGGLE_ID);
    if (!toggle) return null;
    const dropdown = toggle.closest('.dropdown');
    if (!dropdown) return null;
    const menuEl = dropdown.querySelector('.dropdown-menu');
    if (!menuEl) return null;
    return menuEl;
  }

  function mountIfPossible() {
    if (!getSceneIdFromURL()) return false;
    const menuEl = findOperationsMenu();
    if (!menuEl) return false;
    createMenuItem(menuEl);
    return true;
  }

  // Register as a Stash UI task if possible; fallback to menu item.
  if (typeof window.registerTask === 'function') {
    window.registerTask({
      name: 'Transcribe scene (Whisper)',
      description: 'Transcribe the current scene using Whisper',
      icon: 'fa-microphone',
      handler: async () => {
        const sceneId = getSceneIdFromURL();
        if (!sceneId) {
          alert('Whisper Transcribe: could not determine scene id from URL.');
          return;
        }
        await runTranscribe(sceneId);
      },
    });
    console.debug('[WhisperTranscribe] Task registered via registerTask');
  } else {
    // Fallback to original menu item approach.
    mountIfPossible();

    // Observe DOM changes for SPA navigation and render timing
    const observer = new MutationObserver((mutationsList) => {
      for (const mutation of mutationsList) {
        for (const addedNode of mutation.addedNodes) {
          if (addedNode.nodeType !== Node.ELEMENT_NODE) continue;

          // If the operations menu or its toggle appears, attempt mount.
          if (
            addedNode.id === OPERATIONS_TOGGLE_ID ||
            addedNode.querySelector?.(`#${OPERATIONS_TOGGLE_ID}`) ||
            addedNode.classList?.contains('dropdown-menu')
          ) {
            mountIfPossible();
            return;
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // ------------------------------------------------------------------
  // Batch button: transcribe all currently-selected scenes from the grid.
  // ------------------------------------------------------------------
  const BATCH_BTN_ID = 'whisper-transcribe-batch-btn';

  function isSceneListPage() {
    // Matches /scenes and tab variants like /studios/5/scenes, but NOT /scenes/123
    if (/\/scenes\/\d+/.test(window.location.pathname)) return false;
    return /(^|\/)scenes\/?$/.test(window.location.pathname);
  }

  function getSelectedSceneIds() {
    const ids = [];
    const seen = new Set();
    const add = (href) => {
      const m = href && href.match(/\/scenes\/(\d+)/);
      if (m && !seen.has(m[1])) { seen.add(m[1]); ids.push(parseInt(m[1], 10)); }
    };
    document.querySelectorAll('.scene-card, .grid-card').forEach((card) => {
      const cb = card.querySelector('input[type="checkbox"]');
      if ((cb && cb.checked) || card.classList.contains('selected')) {
        const a = card.querySelector('a[href*="/scenes/"]');
        if (a) add(a.getAttribute('href'));
      }
    });
    if (!ids.length) {
      document.querySelectorAll('input[type="checkbox"]:checked').forEach((cb) => {
        const card = cb.closest('.grid-card, .scene-card, .card');
        const a = card && card.querySelector('a[href*="/scenes/"]');
        if (a) add(a.getAttribute('href'));
      });
    }
    return ids;
  }

  async function runTranscribeBatch(ids) {
    const base = document.querySelector('base')?.getAttribute('href') || '/';
    const graphqlURL = new URL('graphql', new URL(base, window.location.href)).toString();
    const resolvedId = await resolvePluginId(graphqlURL);
    if (!resolvedId) {
      alert('Whisper Transcribe plugin not found on server. Reload plugins and refresh the page.');
      return;
    }
    const mutation = `
      mutation RunPluginTask($plugin_id: ID!, $args_map: Map!, $description: String) {
        runPluginTask(plugin_id: $plugin_id, args_map: $args_map, description: $description)
      }
    `;
    let ok = 0;
    for (const id of ids) {
      try {
        const args_map = { mode: 'transcribe_scene_task', scene_id: id };
        const res = await fetch(graphqlURL, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: mutation, variables: { plugin_id: resolvedId, args_map, description: `Whisper: scene ${id}` } }),
        });
        const json = await res.json();
        if (res.ok && !json.errors) ok++;
        else console.error('[WhisperTranscribe] queue failed for scene', id, json.errors);
      } catch (e) {
        console.error('[WhisperTranscribe] queue error for scene', id, e);
      }
    }
    alert(`Whisper: queued ${ok}/${ids.length} scene(s) for transcription.`);
  }

  function ensureBatchButton() {
    const onList = isSceneListPage();
    let btn = document.getElementById(BATCH_BTN_ID);
    if (!onList) { if (btn) btn.remove(); return; }
    if (!btn) {
      btn = document.createElement('button');
      btn.id = BATCH_BTN_ID;
      btn.type = 'button';
      btn.className = 'btn btn-secondary';
      // Offset above the JAV plugin's button so the two FABs don't overlap.
      btn.style.cssText = 'position:fixed;right:20px;bottom:70px;z-index:1050;box-shadow:0 2px 8px rgba(0,0,0,.4);';
      btn.addEventListener('click', async () => {
        const ids = getSelectedSceneIds();
        if (!ids.length) {
          alert('Select one or more scenes first (use the checkboxes), then click this button.');
          return;
        }
        if (!confirm(`Transcribe ${ids.length} selected scene(s) with Whisper (English)?`)) return;
        btn.disabled = true;
        try { await runTranscribeBatch(ids); } finally { btn.disabled = false; }
      });
      document.body.appendChild(btn);
    }
    const n = getSelectedSceneIds().length;
    btn.textContent = n ? `Transcribe ${n} selected (Whisper)` : 'Transcribe selected (Whisper)';
  }

  let batchTimer = null;
  function scheduleEnsureBatch() {
    clearTimeout(batchTimer);
    batchTimer = setTimeout(ensureBatchButton, 150);
  }
  const batchObserver = new MutationObserver(scheduleEnsureBatch);
  batchObserver.observe(document.body, { childList: true, subtree: true });
  document.addEventListener('click', scheduleEnsureBatch, true);
  window.addEventListener('popstate', scheduleEnsureBatch);
  window.addEventListener('hashchange', scheduleEnsureBatch);
  scheduleEnsureBatch();

  console.debug('[WhisperTranscribe] UI script initialized');
})();
