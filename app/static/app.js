const app = {
  state: {
    videos: [],
    calendarVideos: [],
    auditEvents: [],
    videoAuditEvents: [],
    pipelineSummary: null,
    operatorExport: null,
    selectedVideoId: null,
    activePage: 'dashboard',
    assets: [],
    selectedAssetType: null,
    editingAssetType: null,
    readinessByVideoId: {},
    packageDirsByVideoId: {},
    lastComplianceReportByVideoId: {},
    stats: {
      ideas: 0,
      generated: 0,
      approved: 0,
      packages: 0
    }
  },

  async init() {
    this.setupNavigation();
    this.setupWorkspaceSearch();
    this.log('Initializing Local AI Operator Dashboard...', 'info');
    this.setActivePage('dashboard');
    await this.checkHealth();
    await this.loadVideos();
    await this.loadPipelineSummary();
    await this.loadCalendar();
    await this.loadGlobalAudit();
  },

  setupNavigation() {
    const nav = document.getElementById('sidebarNav');
    if (!nav) return;
    nav.querySelectorAll('.nav-item').forEach(item => {
      item.addEventListener('click', () => {
        const page = item.dataset.page;
        if (page) this.setActivePage(page);
      });
    });
  },

  setupWorkspaceSearch() {
    const searchInput = document.getElementById('workspaceSearch');
    if (!searchInput) return;
    searchInput.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      const query = searchInput.value.trim();
      const filterSearch = document.getElementById('filterSearch');
      if (filterSearch) filterSearch.value = query;
      this.setActivePage('content');
      this.loadVideos();
    });
  },

  setActivePage(page) {
    this.state.activePage = page;
    document.querySelectorAll('.page').forEach(section => section.classList.add('hidden'));
    const activeSection = document.getElementById(`page-${page}`);
    if (activeSection) {
      activeSection.classList.remove('hidden');
    }

    document.querySelectorAll('.nav-item').forEach(item => {
      item.classList.toggle('active', item.dataset.page === page);
    });

    const titleMap = {
      dashboard: 'Dashboard',
      content: 'Content',
      assets: 'Assets',
      publishing: 'Publishing',
      compliance: 'Compliance',
      audit: 'Audit'
    };
    const topNavTitle = document.getElementById('topNavTitle');
    if (topNavTitle) topNavTitle.textContent = titleMap[page] || 'Dashboard';
  },

  log(msg, type = 'info') {
    const logContainer = document.getElementById('activityLog');
    if (!logContainer) return;
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    
    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="log-time">[${time}]</span><span class="log-msg">${msg}</span>`;
    
    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;
  },

  getSelectedVideo() {
    if (!this.state.selectedVideoId) return null;
    return this.state.videos.find(video => video.id === this.state.selectedVideoId) || null;
  },

  requireSelectedVideo(actionLabel = 'perform this action') {
    const selected = this.getSelectedVideo();
    if (selected) return selected;
    this.log(`Select a video first to ${actionLabel}.`, 'error');
    return null;
  },

  async checkHealth() {
    try {
      const res = await fetch('/health');
      if (res.ok) {
        document.getElementById('healthIndicator').className = 'health-indicator online';
        document.getElementById('healthText').textContent = 'Backend Online';
        this.log('Backend connection established', 'success');
      } else {
        throw new Error('Health check failed');
      }
    } catch (err) {
      document.getElementById('healthIndicator').className = 'health-indicator offline';
      document.getElementById('healthText').textContent = 'Backend Offline';
      this.log(`Health check failed: ${err.message}`, 'error');
    }
  },

  async loadCalendar() {
    try {
      const res = await fetch('/calendar');
      if (!res.ok) throw new Error('Failed to load calendar');
      const data = await res.json();
      this.state.calendarVideos = data;
      this.renderCalendar();
    } catch (err) {
      this.log(`Error loading calendar: ${err.message}`, 'error');
      document.getElementById('calendarList').innerHTML = `<div class="empty-state">Failed to load calendar.</div>`;
    }
  },

  renderCalendar() {
    const list = document.getElementById('calendarList');
    if (!list) return;
    list.innerHTML = '';

    if (!this.state.calendarVideos || this.state.calendarVideos.length === 0) {
      list.innerHTML = `<div class="empty-state">No scheduled videos.</div>`;
      return;
    }

    this.state.calendarVideos.forEach(video => {
      const card = document.createElement('div');
      card.className = `video-card ${this.state.selectedVideoId === video.id ? 'selected' : ''}`;
      card.onclick = () => this.selectVideo(video.id);

      const pubDateStr = video.publish_date ? new Date(video.publish_date).toLocaleString() : 'Unscheduled';
      const statusLabel = video.publish_status || 'draft';

      card.innerHTML = `
        <div class="video-header">
          <div class="video-title">${video.title}</div>
          <div class="video-status ${statusLabel}">${statusLabel}</div>
        </div>
        <div class="video-details" style="margin-top: 0.5rem;">
          <div class="detail-item" style="grid-column: span 2;">
            <span class="detail-label">Publish Date</span>
            <span class="detail-value" style="color: var(--primary);">${pubDateStr}</span>
          </div>
        </div>
      `;
      list.appendChild(card);
    });
  },

  async loadVideos() {
    try {
      const btn = document.getElementById('btnRefreshVideos');
      if(btn) {
        btn.textContent = 'Loading...';
        btn.disabled = true;
      }

      const search = document.getElementById('filterSearch')?.value || '';
      const status = document.getElementById('filterStatus')?.value || '';
      const pubStatus = document.getElementById('filterPublishStatus')?.value || '';
      
      let queryParams = [];
      if (search) queryParams.push(`search=${encodeURIComponent(search)}`);
      if (status) queryParams.push(`status=${encodeURIComponent(status)}`);
      if (pubStatus) queryParams.push(`publish_status=${encodeURIComponent(pubStatus)}`);
      
      const queryString = queryParams.length ? '?' + queryParams.join('&') : '';
      const res = await fetch('/videos' + queryString);
      if (!res.ok) throw new Error('Failed to fetch videos');
      const data = await res.json();
      
      this.state.videos = data;
      this.renderVideoList();
      this.updateKPIs();
      if (this.state.pipelineSummary) {
        this.renderGuidedFlow(this.state.pipelineSummary);
      }
      this.log(`Loaded ${data.length} videos`, 'success');

      if (this.state.selectedVideoId) {
        const selectedStillVisible = this.state.videos.some(video => video.id === this.state.selectedVideoId);
        if (selectedStillVisible) {
          await this.selectVideo(this.state.selectedVideoId, { fetchAssets: false, clearCompliance: false });
        } else {
          this.clearSelectedVideoUI();
        }
      }
    } catch (err) {
      this.log(`Error loading videos: ${err.message}`, 'error');
      document.getElementById('videoList').innerHTML = `<tr><td colspan="6" class="table-empty">Failed to load videos.</td></tr>`;
    } finally {
      const btn = document.getElementById('btnRefreshVideos');
      if(btn) {
        btn.textContent = 'Refresh';
        btn.disabled = false;
      }
    }
  },

  handleFilterChange() {
    this.loadVideos();
  },

  async loadPipelineSummary() {
    try {
      const res = await fetch('/pipeline/summary');
      if (!res.ok) throw new Error('Failed to load pipeline summary');
      const data = await res.json();
      this.renderPipelineSummary(data);
    } catch (err) {
      this.log(`Error loading pipeline summary: ${err.message}`, 'error');
    }
  },

  renderPipelineSummary(data) {
    this.state.pipelineSummary = data;
    const countsDiv = document.getElementById('pipelineCounts');
    if (countsDiv) {
      const draft = data.status_counts['idea'] || 0;
      const review = data.status_counts['needs_review'] || 0;
      const approved = data.status_counts['approved'] || 0;
      const blocked = data.publish_status_counts['blocked'] || 0;
      
      countsDiv.innerHTML = `
        <div style="background: rgba(255,255,255,0.05); padding: 0.5rem 1rem; border-radius: 4px;"><strong>${data.total_videos}</strong> Total</div>
        <div style="background: rgba(255,255,255,0.05); padding: 0.5rem 1rem; border-radius: 4px; color: var(--warning);"><strong>${review}</strong> Need Review</div>
        <div style="background: rgba(255,255,255,0.05); padding: 0.5rem 1rem; border-radius: 4px; color: var(--success);"><strong>${approved}</strong> Approved</div>
        <div style="background: rgba(255,255,255,0.05); padding: 0.5rem 1rem; border-radius: 4px; color: var(--danger);"><strong>${blocked}</strong> Blocked</div>
      `;
    }

    this.renderActionQueue('actionQueueList', data.action_queue || [], 'Pipeline is clear!');
    this.renderActionQueue('needsAttentionQueueList', data.action_queue || [], 'No urgent actions.');
    this.renderGuidedFlow(data);
  },

  renderActionQueue(containerId, queueItems, emptyMessage) {
    const queueList = document.getElementById(containerId);
    if (!queueList) return;
    queueList.innerHTML = '';
    if (!queueItems || queueItems.length === 0) {
      queueList.innerHTML = `<div class="empty-state">${this.escapeHtml(emptyMessage)}</div>`;
      return;
    }
    queueItems.forEach(action => {
      const item = document.createElement('div');
      item.className = 'video-card';
      item.style.padding = '0.75rem';
      item.onclick = () => this.openQueueAction(action);
      item.innerHTML = `
        <div style="font-weight: 500; font-size: 0.95rem; margin-bottom: 0.25rem;">${this.escapeHtml(action.title)}</div>
        <div style="font-size: 0.8rem; color: var(--textSecondary); margin-bottom: 0.5rem;">${this.escapeHtml(action.reason)}</div>
        <button type="button" class="btn warning queue-action-btn" style="justify-content:flex-start; width:100%; margin-top:0.25rem;" data-action-video-id="${action.video_id}">
          ${this.escapeHtml(action.suggested_next_action || 'Open')}
        </button>
      `;
      const actionBtn = item.querySelector('.queue-action-btn');
      if (actionBtn) {
        actionBtn.onclick = async (event) => {
          event.stopPropagation();
          await this.openQueueAction(action);
        };
      }
      queueList.appendChild(item);
    });
  },

  renderGuidedFlow(summary) {
    const queue = summary.action_queue || [];
    const selectedVideo = this.getSelectedVideo();
    const selectedReadiness = selectedVideo ? this.state.readinessByVideoId[selectedVideo.id] : null;
    const draftCandidate = this.state.videos.find(video => ['idea', 'drafted'].includes(video.status) && !video.approved);
    const reviewCandidate = this.state.videos.find(video => ['needs_review', 'rejected'].includes(video.status) && !video.approved);
    const approvedCandidate = this.state.videos.find(video => video.status === 'approved' && video.approved);
    const packagedCandidate = this.state.videos.find(video => video.status === 'packaged');

    let nextAction = null;
    if (selectedVideo && selectedReadiness && (selectedReadiness.compliance_status === 'blocked' || selectedReadiness.compliance_status === 'warning')) {
      nextAction = {
        text: selectedReadiness.compliance_status === 'blocked'
          ? `${selectedVideo.title}: compliance is blocked. Resolve blockers before approval.`
          : `${selectedVideo.title}: compliance has warnings. Run manual checklist and review carefully.`,
        button: selectedReadiness.compliance_status === 'blocked' ? 'Fix Compliance' : 'Review Compliance',
        handler: async () => {
          await this.selectVideo(selectedVideo.id, { fetchAssets: false, clearCompliance: false });
          this.setActivePage('compliance');
        }
      };
    } else if (draftCandidate) {
      nextAction = {
        text: `${draftCandidate.title}: assets are missing. Generate assets first.`,
        button: 'Generate Assets',
        handler: async () => {
          await this.selectVideo(draftCandidate.id);
          this.setActivePage('assets');
        }
      };
    } else if (reviewCandidate) {
      nextAction = {
        text: `${reviewCandidate.title}: assets are ready but manual review is pending.`,
        button: 'Review and Approve',
        handler: async () => {
          await this.selectVideo(reviewCandidate.id, { fetchAssets: true, clearCompliance: false });
          this.setActivePage('compliance');
        }
      };
    } else if (approvedCandidate) {
      nextAction = {
        text: `${approvedCandidate.title}: approved but not packaged yet.`,
        button: 'Package Video',
        handler: async () => {
          await this.selectVideo(approvedCandidate.id);
          this.setActivePage('assets');
        }
      };
    } else if (packagedCandidate) {
      nextAction = {
        text: `${packagedCandidate.title}: packaged but payload is not ready.`,
        button: 'Prepare Payload',
        handler: async () => {
          await this.selectVideo(packagedCandidate.id);
          this.setActivePage('publishing');
        }
      };
    }

    const nextText = document.getElementById('nextBestActionText');
    const nextButton = document.getElementById('nextBestActionButton');
    const blockersText = document.getElementById('blockersNextStep');
    const dailyChecklist = document.getElementById('dailyChecklistList');
    if (nextText) {
      nextText.textContent = nextAction
        ? nextAction.text
        : 'No urgent actions. Pipeline is clear.';
    }
    if (nextButton) {
      if (nextAction) {
        nextButton.disabled = false;
        nextButton.textContent = nextAction.button || 'Open';
        nextButton.onclick = nextAction.handler || null;
      } else {
        nextButton.disabled = true;
        nextButton.textContent = 'No Action';
        nextButton.onclick = null;
      }
    }
    if (dailyChecklist) {
      const checks = [
        { label: 'Generate assets for ideas', done: !draftCandidate },
        { label: 'Review and approve pending videos', done: (summary.status_counts['needs_review'] || 0) === 0 },
        { label: 'Resolve compliance/publish blockers', done: (summary.publish_status_counts['blocked'] || 0) === 0 },
        { label: 'Package approved videos', done: !approvedCandidate },
        { label: 'Prepare payload for packaged videos', done: !packagedCandidate },
      ];
      dailyChecklist.innerHTML = checks.map(item => `
        <div class="flow-item ${item.done ? 'done' : 'pending'}">${item.done ? '✅' : '•'} ${this.escapeHtml(item.label)}</div>
      `).join('');
    }
    if (blockersText) {
      const blockedItems = queue.filter(item => item.publish_status === 'blocked');
      if (blockedItems.length > 0) {
        blockersText.textContent = `${blockedItems.length} blocked video(s). Resolve blocker details in Compliance or Publishing before moving forward.`;
      } else if (nextAction) {
        blockersText.textContent = `Current focus: ${nextAction.text}`;
      } else {
        blockersText.textContent = 'No blockers. Nothing urgent right now.';
      }
    }
  },

  async openQueueAction(action) {
    if (!action || !action.video_id) return;
    await this.selectVideo(action.video_id, { fetchAssets: true, clearCompliance: false });

    const nextAction = (action.suggested_next_action || '').toLowerCase();
    if (nextAction.includes('review') || nextAction.includes('compliance') || nextAction.includes('approve')) {
      this.setActivePage('compliance');
    } else if (nextAction.includes('publish') || nextAction.includes('payload') || nextAction.includes('schedule')) {
      this.setActivePage('publishing');
    } else {
      this.setActivePage('assets');
    }
  },

  updateKPIs() {
    let ideas = this.state.videos.length;
    let approved = this.state.videos.filter(v => v.approved).length;
    let generated = this.state.videos.filter(v => ['needs_review', 'approved', 'packaged', 'publish_ready', 'published'].includes(v.status)).length;
    let packages = this.state.videos.filter(v => ['packaged', 'publish_ready', 'published'].includes(v.status)).length;

    document.getElementById('kpiTotalIdeas').textContent = ideas;
    document.getElementById('kpiGeneratedAssets').textContent = generated;
    document.getElementById('kpiApprovedVideos').textContent = approved;
    document.getElementById('kpiPackagesCreated').textContent = packages;
  },

  renderVideoList() {
    const list = document.getElementById('videoList');
    list.innerHTML = '';

    if (this.state.videos.length === 0) {
      list.innerHTML = `<tr><td colspan="6" class="table-empty">No video ideas found.</td></tr>`;
      return;
    }

    this.state.videos.forEach(video => {
      const row = document.createElement('tr');
      row.className = `content-row ${this.state.selectedVideoId === video.id ? 'selected' : ''}`;
      row.onclick = () => this.selectVideo(video.id);

      const workflowStatus = video.status || 'idea';
      const publishStatus = video.publish_status || 'draft';

      row.innerHTML = `
        <td>
          <div class="video-title-main">${this.escapeHtml(video.title || 'Untitled')}</div>
          <div class="video-meta-line">${this.escapeHtml(video.pillar || 'No pillar')} • ${this.escapeHtml(video.pain_point || 'No pain point')}</div>
        </td>
        <td><span class="video-status ${workflowStatus}">${this.escapeHtml(workflowStatus)}</span></td>
        <td><span class="video-status ${publishStatus}">${this.escapeHtml(publishStatus)}</span></td>
        <td>${this.escapeHtml(video.niche || '—')}</td>
        <td>${this.escapeHtml(video.target_audience || '—')}</td>
        <td>
          <div class="row-actions">
            <button class="btn" data-action="select">Select</button>
            <button class="btn" data-action="edit">Edit</button>
            <button class="btn danger" data-action="delete">Delete</button>
          </div>
        </td>
      `;

      const selectBtn = row.querySelector('[data-action=\"select\"]');
      if (selectBtn) {
        selectBtn.onclick = (event) => {
          event.stopPropagation();
          this.selectVideo(video.id);
          this.setActivePage('assets');
        };
      }

      const editBtn = row.querySelector('[data-action=\"edit\"]');
      if (editBtn) {
        editBtn.onclick = (event) => {
          event.stopPropagation();
          this.selectVideo(video.id, { fetchAssets: false, clearCompliance: false });
          this.openEditModal();
        };
      }

      const deleteBtn = row.querySelector('[data-action=\"delete\"]');
      if (deleteBtn) {
        deleteBtn.onclick = (event) => {
          event.stopPropagation();
          this.selectVideo(video.id, { fetchAssets: false, clearCompliance: false });
          this.openDeleteModal();
        };
      }

      list.appendChild(row);
    });
  },

  async selectVideo(id, options = {}) {
    if (typeof options === 'boolean') {
      options = { fetchAssets: options };
    }
    const {
      fetchAssets = true,
      clearCompliance = true,
      reloadAudit = true
    } = options;

    this.state.selectedVideoId = id;
    this.renderVideoList();
    this.renderCalendar();

    const video = this.state.videos.find(v => v.id === id);
    if (!video) {
      this.clearSelectedVideoUI();
      return;
    }

    const selectedVideoTitle = document.getElementById('selectedVideoTitle');
    if (selectedVideoTitle) selectedVideoTitle.textContent = `Selected Video: ${video.title}`;
    const complianceSelected = document.getElementById('complianceSelectedVideo');
    if (complianceSelected) complianceSelected.textContent = video.title;

    const selectedVideoActions = document.getElementById('selectedVideoActions');
    const metadataDisplay = document.getElementById('metadataDisplay');
    if (selectedVideoActions) selectedVideoActions.style.display = 'flex';
    if (metadataDisplay) metadataDisplay.classList.remove('hidden');

    const emptyText = '<span class="empty-state-text">Not set</span>';
    const metaNiche = document.getElementById('metaNiche');
    const metaAudience = document.getElementById('metaAudience');
    const metaAngle = document.getElementById('metaAngle');
    const metaNotes = document.getElementById('metaNotes');
    if (metaNiche) metaNiche.innerHTML = video.niche || emptyText;
    if (metaAudience) metaAudience.innerHTML = video.target_audience || emptyText;
    if (metaAngle) metaAngle.innerHTML = video.angle || emptyText;
    if (metaNotes) metaNotes.innerHTML = video.notes || emptyText;

    const readinessPanel = document.getElementById('readinessPanel');
    const publishingSettings = document.getElementById('publishingSettings');
    if (readinessPanel) readinessPanel.classList.remove('hidden');
    if (publishingSettings) publishingSettings.classList.remove('hidden');

    const pubStatus = document.getElementById('pubStatus');
    if (pubStatus) pubStatus.value = video.publish_status || 'draft';

    const pubDate = document.getElementById('pubDate');
    if (pubDate) {
      if (video.publish_date) {
        const d = new Date(video.publish_date);
        d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
        pubDate.value = d.toISOString().slice(0, 16);
      } else {
        pubDate.value = '';
      }
    }

    const pubNotes = document.getElementById('pubNotes');
    if (pubNotes) pubNotes.value = video.publish_notes || '';
    const pubError = document.getElementById('pubError');
    if (pubError) pubError.style.display = 'none';

    const compliancePanel = document.getElementById('compliancePanel');
    const auditPanel = document.getElementById('auditPanel');
    if (compliancePanel) compliancePanel.classList.remove('hidden');
    if (auditPanel) auditPanel.classList.remove('hidden');

    if (clearCompliance) {
      this.clearComplianceResults();
    } else if (this.state.lastComplianceReportByVideoId[id]) {
      this.renderComplianceReport(this.state.lastComplianceReportByVideoId[id]);
    } else {
      this.clearComplianceResults();
    }

    if (fetchAssets) {
      await this.loadAssets();
    }

    await this.loadReadiness();
    if (reloadAudit) {
      await this.loadVideoAudit();
    }
    this.updateWorkflowAndCTA(video);
  },

  clearSelectedVideoUI() {
    this.state.selectedVideoId = null;
    this.state.assets = [];
    this.state.selectedAssetType = null;
    this.state.videoAuditEvents = [];
    const selectedVideoTitle = document.getElementById('selectedVideoTitle');
    if (selectedVideoTitle) selectedVideoTitle.textContent = 'Selected Video: None';
    const complianceSelected = document.getElementById('complianceSelectedVideo');
    if (complianceSelected) complianceSelected.textContent = 'None selected';
    const selectedVideoActions = document.getElementById('selectedVideoActions');
    const metadataDisplay = document.getElementById('metadataDisplay');
    const readinessPanel = document.getElementById('readinessPanel');
    const publishingSettings = document.getElementById('publishingSettings');
    const auditPanel = document.getElementById('auditPanel');
    if (selectedVideoActions) selectedVideoActions.style.display = 'none';
    if (metadataDisplay) metadataDisplay.classList.add('hidden');
    if (readinessPanel) readinessPanel.classList.add('hidden');
    if (publishingSettings) publishingSettings.classList.add('hidden');
    if (auditPanel) auditPanel.classList.add('hidden');
    const ctaPanel = document.getElementById('ctaPanel');
    if (ctaPanel) ctaPanel.innerHTML = '<div class="empty-state" style="height: auto; padding: 1rem;">Select a video to see actions</div>';
    this.clearComplianceResults();
    this.renderAssets();
    this.renderVideoAudit();
  },

  updateWorkflowAndCTA(video) {
    const steps = ['idea', 'generated', 'approved', 'packaged', 'payload_ready'];
    const readiness = this.state.readinessByVideoId?.[video.id];

    let currentStepIndex = 0;
    if (video.status === 'needs_review' || video.status === 'rejected' || video.status === 'approved' || video.status === 'packaged' || video.status === 'publish_ready' || video.status === 'published') currentStepIndex = 1;
    if (video.approved || video.status === 'approved' || video.status === 'packaged' || video.status === 'publish_ready' || video.status === 'published') currentStepIndex = 2;
    if (video.status === 'packaged' || video.status === 'publish_ready' || video.status === 'published') currentStepIndex = 3;
    if (video.status === 'publish_ready' || video.status === 'published') currentStepIndex = 4;

    // Update Progress Bar
    steps.forEach((step, idx) => {
      const el = document.getElementById(`step-${step}`);
      if (el) {
        el.className = 'step';
        if (idx < currentStepIndex) el.classList.add('completed');
        if (idx === currentStepIndex) el.classList.add('active');
      }
    });

    const connectors = document.querySelectorAll('.step-connector');
    connectors.forEach((conn, idx) => {
      conn.className = 'step-connector';
      if (idx < currentStepIndex) conn.classList.add('completed');
    });

    // Update Dynamic CTA
    const ctaPanel = document.getElementById('ctaPanel');
    ctaPanel.innerHTML = '';
    
    if (currentStepIndex === 0) {
      ctaPanel.innerHTML = `<button class="btn primary" id="btnDynamic" onclick="app.generateAll()">Generate All</button>`;
    } else if (currentStepIndex === 1) {
      ctaPanel.innerHTML = `<button class="btn warning" id="btnDynamic" onclick="app.openReviewModal()">Review and Approve</button>`;
    } else if (readiness?.compliance_status === 'blocked') {
      ctaPanel.innerHTML = `<button class="btn danger" id="btnDynamic" onclick="app.setActivePage('compliance')">Fix Compliance Blockers</button>`;
    } else if (currentStepIndex === 2) {
      ctaPanel.innerHTML = `<button class="btn primary" id="btnDynamic" onclick="app.packageAssets()">Package</button>`;
    } else if (currentStepIndex === 3) {
      ctaPanel.innerHTML = `<button class="btn primary" id="btnDynamic" onclick="app.prepareYtPayload()">Prepare YouTube Payload</button>`;
    } else if (currentStepIndex >= 4) {
      ctaPanel.innerHTML = `<button class="btn success" id="btnDynamic" disabled>Workflow Complete</button>`;
    }

    // Update Package Info display
    const packageInfo = document.getElementById('packageInfo');
    const packageDir = this.state.packageDirsByVideoId[video.id];
    if (packageDir) {
      packageInfo.classList.remove('hidden');
      document.getElementById('packagePathText').textContent = packageDir;
      document.getElementById('openCommandText').textContent = `open "${packageDir}"`;
    } else {
      packageInfo.classList.add('hidden');
    }
  },

  async actionWrapper(btnId, actionName, fetchOptions, urlFn, successMsgFn, refreshOptions = {}) {
    const selected = this.requireSelectedVideo(actionName.toLowerCase());
    if (!selected) return null;

    const btn = document.getElementById(btnId);
    if(btn) {
      btn.classList.add('loading');
      btn.disabled = true;
    }

    try {
      this.log(`Starting: ${actionName}...`, 'info');
      const url = urlFn(this.state.selectedVideoId);
      const res = await fetch(url, fetchOptions);
      
      const data = await res.json().catch(() => ({}));
      
      if (!res.ok) {
        throw new Error(data.detail || `Request failed with status ${res.status}`);
      }

      this.log(successMsgFn ? successMsgFn(data) : `${actionName} completed successfully.`, 'success');
      await this.refreshAfterMutation(refreshOptions);
      return data;
    } catch (err) {
      this.log(`${actionName} failed: ${err.message}`, 'error');
      return null;
    } finally {
      if(btn) {
        btn.classList.remove('loading');
        btn.disabled = false;
      }
    }
  },

  async refreshAfterMutation(options = {}) {
    const {
      reloadAssets = true,
      reloadCalendar = true,
      reloadAudit = true,
      keepComplianceReport = false
    } = options;

    await this.loadVideos();
    await this.loadPipelineSummary();
    if (reloadCalendar) {
      await this.loadCalendar();
    }
    if (reloadAudit) {
      await this.loadGlobalAudit();
    }
    if (this.state.selectedVideoId) {
      await this.selectVideo(this.state.selectedVideoId, {
        fetchAssets: reloadAssets,
        clearCompliance: !keepComplianceReport,
        reloadAudit
      });
    }
  },

  generateAll() {
    this.actionWrapper(
      'btnDynamic',
      'Generate All Assets',
      { method: 'POST' },
      id => `/videos/${id}/generate`,
      () => 'Generated all assets.',
      { reloadAssets: true, keepComplianceReport: false }
    );
  },

  async loadAssets() {
    if (!this.state.selectedVideoId) return;

    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}/assets`);
      if (!res.ok) throw new Error('Failed to load assets');
      
      const assets = await res.json();
      this.state.assets = assets || [];
      
      if (this.state.assets.length > 0) {
        if (!this.state.selectedAssetType) {
          this.state.selectedAssetType = this.state.assets[0].asset_type;
        }
      } else {
        this.state.selectedAssetType = null;
      }

      this.renderAssets();
    } catch (err) {
      this.log(`Error loading assets: ${err.message}`, 'error');
      document.getElementById('assetTabs').innerHTML = '';
      document.getElementById('assetsViewer').innerHTML = `<div class="empty-state">Error loading assets.</div>`;
    }
  },

  renderAssets() {
    const tabsContainer = document.getElementById('assetTabs');
    const viewer = document.getElementById('assetsViewer');
    if (!tabsContainer || !viewer) return;
    
    tabsContainer.innerHTML = '';
    viewer.innerHTML = '';

    if (this.state.assets.length === 0) {
      viewer.innerHTML = `<div class="empty-state">No assets generated yet.</div>`;
      return;
    }

    // Create Tabs
    this.state.assets.forEach(asset => {
      const tab = document.createElement('div');
      tab.className = `asset-tab ${this.state.selectedAssetType === asset.asset_type ? 'active' : ''}`;
      tab.textContent = asset.asset_type;
      tab.onclick = () => {
        this.state.selectedAssetType = asset.asset_type;
        this.renderAssets();
      };
      tabsContainer.appendChild(tab);
    });

    // Create Content
    const selectedAsset = this.state.assets.find(a => a.asset_type === this.state.selectedAssetType);
    if (selectedAsset) {
      const bodyId = `asset-body-${selectedAsset.asset_type}`;
      viewer.innerHTML = `
        <div class="asset-toolbar">
          <span style="font-size: 0.75rem; color: var(--textMuted); margin-right: auto; line-height: 2;">
            v${selectedAsset.version} • ${new Date(selectedAsset.created_at).toLocaleString()}
          </span>
          <button class="btn copy-btn" onclick="app.regenerateAsset('${selectedAsset.asset_type}')">Regenerate</button>
          <button class="btn copy-btn" onclick="app.openEditAssetModal('${selectedAsset.asset_type}')">Edit</button>
          <button class="btn copy-btn" onclick="app.copyAssetContent('${bodyId}')">Copy Content</button>
        </div>
        <div class="asset-body" id="${bodyId}">${this.escapeHtml(selectedAsset.body)}</div>
      `;
    }
  },

  openReviewModal() {
    const selected = this.requireSelectedVideo('open manual review checklist');
    if (!selected) return;
    const readiness = this.state.readinessByVideoId[selected.id];

    document.querySelectorAll('.check-item input[type="checkbox"]').forEach(cb => cb.checked = false);
    const approveBtn = document.getElementById('btnConfirmApprove');
    const rejectBtn = document.getElementById('btnConfirmReject');
    if (approveBtn) approveBtn.disabled = true;
    if (rejectBtn) rejectBtn.disabled = false;

    const notice = document.getElementById('reviewComplianceNotice');
    if (notice) {
      if (readiness?.compliance_status === 'blocked') {
        notice.style.display = 'block';
        notice.style.backgroundColor = 'rgba(248, 113, 113, 0.1)';
        notice.style.color = 'var(--danger)';
        notice.style.borderColor = 'rgba(248, 113, 113, 0.3)';
        notice.innerHTML = '<strong>Compliance Blocked:</strong> Approval is disabled until blockers are fixed.';
      } else if (readiness?.compliance_status === 'warning') {
        notice.style.display = 'block';
        notice.style.backgroundColor = 'rgba(245, 158, 11, 0.1)';
        notice.style.color = 'var(--warning)';
        notice.style.borderColor = 'rgba(245, 158, 11, 0.3)';
        notice.innerHTML = '<strong>Compliance Warning:</strong> Approval is allowed after manual checklist confirmation.';
      } else {
        notice.style.display = 'none';
      }
    }
    document.getElementById('reviewModal').classList.remove('hidden');
  },

  closeReviewModal() {
    document.getElementById('reviewModal').classList.add('hidden');
  },

  checkApprovalReady() {
    const selected = this.getSelectedVideo();
    const readiness = selected ? this.state.readinessByVideoId[selected.id] : null;
    const checkboxes = document.querySelectorAll('.check-item input[type="checkbox"]');
    const allChecked = Array.from(checkboxes).every(cb => cb.checked);
    const blocked = readiness?.compliance_status === 'blocked';
    const approveBtn = document.getElementById('btnConfirmApprove');
    if (approveBtn) approveBtn.disabled = blocked || !allChecked;
  },

  async submitReviewDecision(passed) {
    const selected = this.requireSelectedVideo('submit manual review');
    if (!selected) return;
    const readiness = this.state.readinessByVideoId[selected.id];
    if (passed && readiness?.compliance_status === 'blocked') {
      this.log('Cannot approve: compliance status is blocked.', 'error');
      return;
    }

    const btnId = passed ? 'btnConfirmApprove' : 'btnConfirmReject';
    const modalBtn = document.getElementById(btnId);
    if (modalBtn) {
      modalBtn.classList.add('loading');
      modalBtn.disabled = true;
    }
    try {
      this.log(`${passed ? 'Approving' : 'Rejecting'} video...`, 'info');
      const res = await fetch(`/videos/${selected.id}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          passed,
          reviewer: 'operator',
          notes: passed ? 'Manual checklist confirmed by operator.' : 'Rejected during manual checklist review.'
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `Review request failed with status ${res.status}`);
      }
      this.log(passed ? 'Video approved for packaging.' : 'Video rejected and moved to needs revision state.', 'success');
      this.closeReviewModal();
      await this.refreshAfterMutation({ reloadAssets: true, reloadCalendar: true, reloadAudit: true, keepComplianceReport: false });
      this.setActivePage(passed ? 'assets' : 'compliance');
    } catch (err) {
      this.log(`${passed ? 'Approve' : 'Reject'} failed: ${err.message}`, 'error');
    } finally {
      if (modalBtn) {
        modalBtn.classList.remove('loading');
      }
      this.checkApprovalReady();
      const rejectBtn = document.getElementById('btnConfirmReject');
      if (rejectBtn) rejectBtn.disabled = false;
    }
  },

  confirmApprove() {
    return this.submitReviewDecision(true);
  },

  confirmReject() {
    return this.submitReviewDecision(false);
  },

  async loadReadiness() {
    if (!this.state.selectedVideoId) return;
    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}/readiness`);
      if (!res.ok) throw new Error('Failed to load readiness');
      const data = await res.json();
      this.state.readinessByVideoId[this.state.selectedVideoId] = data;
      this.renderReadiness(data);
      return data;
    } catch (err) {
      this.log(`Error loading readiness: ${err.message}`, 'error');
      return null;
    }
  },

  renderReadiness(readiness) {
    const list = document.getElementById('readinessChecklist');
    const warningsBox = document.getElementById('readinessWarnings');
    
    list.innerHTML = '';
    warningsBox.innerHTML = '';

    const checks = [
      { key: 'assets_generated', label: 'Assets Generated' },
      { key: 'review_approved', label: 'Manually Reviewed & Approved' },
      { key: 'package_created', label: 'Assets Packaged' },
      { key: 'youtube_metadata_prepared', label: 'YouTube Payload Prepared' },
      { key: 'publish_date_set', label: 'Publish Date Set' }
    ];

    checks.forEach(check => {
      const isPass = readiness[check.key];
      const icon = isPass ? '✅' : '❌';
      const color = isPass ? 'var(--success)' : 'var(--textMuted)';
      
      const item = document.createElement('div');
      item.style.display = 'flex';
      item.style.alignItems = 'center';
      item.style.gap = '0.5rem';
      item.style.fontSize = '0.9rem';
      item.style.color = color;
      
      item.innerHTML = `<span>${icon}</span> <span>${check.label}</span>`;
      list.appendChild(item);
    });

    const warnings = readiness.blocking_reasons || [];
    if (warnings.length > 0) {
      warnings.forEach(w => {
        const wItem = document.createElement('div');
        wItem.style.color = 'var(--warning)';
        wItem.style.fontSize = '0.85rem';
        wItem.textContent = `⚠️ ${w}`;
        warningsBox.appendChild(wItem);
      });
    }

    const previewPlaceholderText = document.getElementById('previewPlaceholderText');
    if (previewPlaceholderText) {
      previewPlaceholderText.textContent = 'No rendered video preview yet.';
    }

    // Update Review Modal compliance notice
    const notice = document.getElementById('reviewComplianceNotice');
    if (notice) {
      if (readiness.compliance_status === 'blocked') {
        notice.style.display = 'block';
        notice.style.backgroundColor = 'rgba(248, 113, 113, 0.1)';
        notice.style.color = 'var(--danger)';
        notice.style.borderColor = 'rgba(248, 113, 113, 0.3)';
        notice.innerHTML = `<strong>⚠️ Compliance Blocked:</strong> Cannot approve. Fix compliance errors first.`;
      } else if (readiness.compliance_status === 'warning') {
        notice.style.display = 'block';
        notice.style.backgroundColor = 'rgba(245, 158, 11, 0.1)';
        notice.style.color = 'var(--warning)';
        notice.style.borderColor = 'rgba(245, 158, 11, 0.3)';
        notice.innerHTML = `<strong>⚠️ Compliance Warning:</strong> Warnings detected. Review carefully.`;
      } else {
        notice.style.display = 'none';
      }
    }
  },

  async runComplianceCheck() {
    const selected = this.requireSelectedVideo('run compliance check');
    if (!selected) return;
    
    const btn = document.getElementById('btnRunCompliance');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Running...';
    }

    try {
      this.log('Running compliance checks...', 'info');
      const res = await fetch(`/videos/${selected.id}/compliance/run`, {
        method: 'POST'
      });
      const data = await res.json().catch(() => ({}));
      
      if (!res.ok) {
        throw new Error(data.detail || 'Compliance check failed');
      }

      this.log(`Compliance check completed: ${data.overall_status}`, 'success');
      this.state.lastComplianceReportByVideoId[selected.id] = data;
      this.renderComplianceReport(data);
      const staleNotice = document.getElementById('complianceStaleNotice');
      if (staleNotice) staleNotice.style.display = 'none';
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: false, reloadAudit: true, keepComplianceReport: true });
    } catch (err) {
      this.log(`Compliance error: ${err.message}`, 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Run Check';
      }
    }
  },

  renderComplianceReport(report) {
    const resultsDiv = document.getElementById('complianceResults');
    const badge = document.getElementById('complianceOverallBadge');
    if (!resultsDiv || !badge) return;
    
    badge.textContent = report.overall_status.toUpperCase();
    badge.className = 'status-badge'; 
    
    if (report.overall_status === 'pass') {
      badge.style.background = 'rgba(52, 211, 153, 0.1)';
      badge.style.color = 'var(--success)';
      badge.style.border = '1px solid rgba(52, 211, 153, 0.3)';
    } else if (report.overall_status === 'warning') {
      badge.style.background = 'rgba(245, 158, 11, 0.1)';
      badge.style.color = 'var(--warning)';
      badge.style.border = '1px solid rgba(245, 158, 11, 0.3)';
    } else if (report.overall_status === 'blocked') {
      badge.style.background = 'rgba(248, 113, 113, 0.1)';
      badge.style.color = 'var(--danger)';
      badge.style.border = '1px solid rgba(248, 113, 113, 0.3)';
    } else {
      badge.style.background = 'var(--bg-dark)';
      badge.style.color = 'var(--textSecondary)';
      badge.style.border = '1px solid var(--border)';
    }

    resultsDiv.innerHTML = '';
    
    if (!report.checks || report.checks.length === 0) {
      resultsDiv.innerHTML = '<div class="empty-state" style="padding: 1rem 0;">No checks returned.</div>';
      return;
    }

    report.checks.forEach(check => {
      let icon = '✅';
      let colorClass = 'pass-check';
      
      if (check.status === 'blocked') {
        icon = '🚫';
        colorClass = 'blocked-check';
      } else if (check.status === 'warning') {
        icon = '⚠️';
        colorClass = 'warning-check';
      }

      const div = document.createElement('div');
      div.className = `compliance-item ${colorClass}`;
      div.innerHTML = `
        <div class="compliance-header">
          <span class="compliance-icon">${icon}</span>
          <span class="compliance-label">${this.escapeHtml(check.label)}</span>
          ${check.asset_type ? `<span class="compliance-asset-type">${this.escapeHtml(check.asset_type)}</span>` : ''}
        </div>
        <div class="compliance-detail">${this.escapeHtml(check.detail)}</div>
        ${check.suggested_fix ? `<div class="compliance-fix"><strong>Fix:</strong> ${this.escapeHtml(check.suggested_fix)}</div>` : ''}
      `;
      resultsDiv.appendChild(div);
    });
  },

  clearComplianceResults() {
    const badge = document.getElementById('complianceOverallBadge');
    if (badge) {
      badge.textContent = 'Untested';
      badge.style.background = 'var(--bg-dark)';
      badge.style.color = 'var(--textSecondary)';
      badge.style.border = '1px solid var(--border)';
    }
    const resultsDiv = document.getElementById('complianceResults');
    if (resultsDiv) {
      resultsDiv.innerHTML = '<div class="empty-state" style="padding: 1rem 0;">Run a check to view compliance details.</div>';
    }
    const stale = document.getElementById('complianceStaleNotice');
    if (stale) stale.style.display = 'none';
  },

  async savePublishing() {
    const selected = this.requireSelectedVideo('save publishing settings');
    if (!selected) return;
    
    const btn = document.getElementById('btnSavePublishing');
    const errSpan = document.getElementById('pubError');
    btn.disabled = true;
    errSpan.style.display = 'none';

    let pubDate = document.getElementById('pubDate').value;
    if (pubDate) {
      pubDate = new Date(pubDate).toISOString();
    } else {
      pubDate = null;
    }

    const payload = {
      publish_status: document.getElementById('pubStatus').value,
      publish_date: pubDate,
      publish_notes: document.getElementById('pubNotes').value.trim() || null
    };

    try {
      const res = await fetch(`/videos/${selected.id}/publishing`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      
      const data = await res.json().catch(() => ({}));
      
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to update publishing settings');
      }
      
      this.log('Publishing settings saved successfully.', 'success');
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true });
    } catch (err) {
      this.log(`Failed to save publishing: ${err.message}`, 'error');
      errSpan.textContent = err.message;
      errSpan.style.display = 'block';
    } finally {
      btn.disabled = false;
    }
  },

  packageAssets() {
    this.actionWrapper(
      'btnDynamic',
      'Package Assets',
      { method: 'POST' },
      id => `/videos/${id}/package`,
      (data) => {
        if (data && data.package_dir && this.state.selectedVideoId) {
          this.state.packageDirsByVideoId[this.state.selectedVideoId] = data.package_dir;
        }
        return 'Video assets packaged successfully.';
      },
      { reloadAssets: true, reloadCalendar: true, reloadAudit: true, keepComplianceReport: false }
    );
  },

  prepareYtPayload() {
    this.actionWrapper(
      'btnDynamic',
      'Prepare YT Payload',
      { method: 'POST' },
      id => `/publish/${id}/prepare-youtube-payload`,
      () => 'YouTube payload prepared successfully.',
      { reloadAssets: false, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true }
    );
  },

  async copyPackagePath() {
    const text = document.getElementById('packagePathText').textContent;
    if (!text) {
      this.log('No package path available to copy.', 'error');
      return;
    }
    await this.copyToClipboard(text, 'Package path copied to clipboard.');
  },

  async copyOpenCommand() {
    const text = document.getElementById('openCommandText').textContent;
    if (!text) {
      this.log('No open command available to copy.', 'error');
      return;
    }
    await this.copyToClipboard(text, 'Command copied to clipboard.');
  },

  async copyAssetContent(elementId) {
    const el = document.getElementById(elementId);
    if (!el) return;
    
    // We want the text content since we escaped HTML
    await this.copyToClipboard(el.textContent, 'Asset content copied to clipboard.');
  },

  async copyToClipboard(text, successMsg) {
    try {
      if (!navigator?.clipboard?.writeText) {
        throw new Error('Clipboard API unavailable in this browser context');
      }
      await navigator.clipboard.writeText(text);
      this.log(successMsg, 'success');
    } catch (err) {
      this.log(`Failed to copy: ${err.message}`, 'error');
    }
  },

  // Modal Handlers
  openNewIdeaModal() {
    document.getElementById('newIdeaTitle').value = '';
    document.getElementById('newIdeaNiche').value = '';
    document.getElementById('newIdeaAudience').value = '';
    document.getElementById('newIdeaAngle').value = '';
    document.getElementById('newIdeaNotes').value = '';
    document.getElementById('newIdeaModal').classList.remove('hidden');
  },

  closeNewIdeaModal() {
    document.getElementById('newIdeaModal').classList.add('hidden');
  },

  async submitNewIdea() {
    const title = document.getElementById('newIdeaTitle').value.trim();
    if (!title) return alert('Title is required');

    const payload = {
      channel_id: 1, // Defaulting to 1 for this phase
      title: title,
      niche: document.getElementById('newIdeaNiche').value.trim() || null,
      target_audience: document.getElementById('newIdeaAudience').value.trim() || null,
      angle: document.getElementById('newIdeaAngle').value.trim() || null,
      notes: document.getElementById('newIdeaNotes').value.trim() || null
    };

    try {
      const res = await fetch('/videos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error('Failed to create video idea');
      this.log('Created new video idea', 'success');
      this.closeNewIdeaModal();
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true });
    } catch (err) {
      this.log(`Error creating idea: ${err.message}`, 'error');
    }
  },

  openBatchModal() {
    document.getElementById('batchIdeaText').value = '';
    document.getElementById('batchPreview').style.display = 'none';
    document.getElementById('btnSubmitBatch').disabled = true;
    document.getElementById('batchIdeaModal').classList.remove('hidden');
  },

  closeBatchModal() {
    document.getElementById('batchIdeaModal').classList.add('hidden');
  },

  parseBatchText() {
    const text = document.getElementById('batchIdeaText').value.trim();
    if (!text) return [];
    
    const lines = text.split('\\n');
    const parsed = [];
    
    for (let line of lines) {
      if (!line.trim()) continue;
      const parts = line.split('|').map(s => s.trim());
      if (parts.length > 0 && parts[0]) {
        parsed.push({
          title: parts[0],
          thumbnail_text: parts.length > 1 ? parts[1] : null,
          pillar: parts.length > 2 ? parts[2] : null,
          target_view: parts.length > 3 ? parts[3] : null
        });
      }
    }
    return parsed;
  },

  previewBatch() {
    const parsed = this.parseBatchText();
    const previewBox = document.getElementById('batchPreview');
    const submitBtn = document.getElementById('btnSubmitBatch');
    
    if (parsed.length === 0) {
      previewBox.style.display = 'none';
      submitBtn.disabled = true;
      return;
    }
    
    let html = `<strong>${parsed.length} ideas to import:</strong><ul style="margin-top: 0.5rem; padding-left: 1.5rem; color: var(--textSecondary);">`;
    parsed.forEach(p => {
      html += `<li><strong>${this.escapeHtml(p.title)}</strong> (Thumb: ${this.escapeHtml(p.thumbnail_text || 'none')})</li>`;
    });
    html += '</ul>';
    
    previewBox.innerHTML = html;
    previewBox.style.display = 'block';
    submitBtn.disabled = false;
  },

  async submitBatch() {
    const parsed = this.parseBatchText();
    if (parsed.length === 0) return;
    
    const submitBtn = document.getElementById('btnSubmitBatch');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Importing...';
    
    const payload = {
      channel_id: 1, // Defaulting to 1 for this phase
      videos: parsed
    };

    try {
      const res = await fetch('/videos/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || 'Failed to import batch');
      }
      this.log(`Successfully imported ${parsed.length} video ideas`, 'success');
      this.closeBatchModal();
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true });
    } catch (err) {
      this.log(`Error importing batch: ${err.message}`, 'error');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Import Batch';
    }
  },

  openEditModal() {
    const video = this.requireSelectedVideo('edit video metadata');
    if (!video) return;

    document.getElementById('editTitle').value = video.title || '';
    document.getElementById('editNiche').value = video.niche || '';
    document.getElementById('editAudience').value = video.target_audience || '';
    document.getElementById('editAngle').value = video.angle || '';
    document.getElementById('editNotes').value = video.notes || '';

    document.getElementById('editModal').classList.remove('hidden');
  },

  closeEditModal() {
    document.getElementById('editModal').classList.add('hidden');
  },

  async submitEdit() {
    const selected = this.requireSelectedVideo('save video metadata');
    if (!selected) return;

    const payload = {
      title: document.getElementById('editTitle').value.trim() || null,
      niche: document.getElementById('editNiche').value.trim() || null,
      target_audience: document.getElementById('editAudience').value.trim() || null,
      angle: document.getElementById('editAngle').value.trim() || null,
      notes: document.getElementById('editNotes').value.trim() || null
    };

    try {
      const res = await fetch(`/videos/${selected.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error('Failed to update video');
      this.log('Updated video metadata', 'success');
      this.closeEditModal();
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true });
    } catch (err) {
      this.log(`Error updating video: ${err.message}`, 'error');
    }
  },

  openDeleteModal() {
    const selected = this.requireSelectedVideo('delete a video');
    if (!selected) return;
    document.getElementById('deleteModal').classList.remove('hidden');
  },

  closeDeleteModal() {
    document.getElementById('deleteModal').classList.add('hidden');
  },

  async confirmDelete() {
    const selected = this.requireSelectedVideo('delete a video');
    if (!selected) return;
    
    try {
      const res = await fetch(`/videos/${selected.id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Failed to delete video');
      
      this.log('Deleted video idea', 'success');
      this.closeDeleteModal();
      this.clearSelectedVideoUI();
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: true, reloadAudit: true, keepComplianceReport: false });
    } catch (err) {
      this.log(`Error deleting video: ${err.message}`, 'error');
    }
  },

  openEditAssetModal(assetType) {
    const selected = this.requireSelectedVideo('edit an asset');
    if (!selected) return;
    const asset = this.state.assets.find(a => a.asset_type === assetType);
    if (!asset) {
      this.log(`No ${assetType} asset found for selected video.`, 'error');
      return;
    }

    this.state.editingAssetType = assetType;
    document.getElementById('editAssetTypeLabel').textContent = assetType;
    document.getElementById('editAssetBody').value = asset.body;
    document.getElementById('editAssetModal').classList.remove('hidden');
  },

  closeEditAssetModal() {
    document.getElementById('editAssetModal').classList.add('hidden');
    this.state.editingAssetType = null;
  },

  async submitEditAsset() {
    const selected = this.requireSelectedVideo('save asset edits');
    if (!selected || !this.state.editingAssetType) return;
    
    const bodyText = document.getElementById('editAssetBody').value;
    const assetType = this.state.editingAssetType;

    const payload = {
      body: bodyText
    };

    try {
      const res = await fetch(`/videos/${selected.id}/assets/${assetType}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      
      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to update asset');
      }
      
      this.log(`Asset ${assetType} updated successfully`, 'success');
      this.closeEditAssetModal();
      await this.refreshAfterMutation({ reloadAssets: true, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true });
      const staleNotice = document.getElementById('complianceStaleNotice');
      if (staleNotice) staleNotice.style.display = 'inline';
    } catch (err) {
      this.log(`Error updating asset: ${err.message}`, 'error');
    }
  },

  async regenerateAsset(assetType) {
    const selected = this.requireSelectedVideo(`regenerate ${assetType} asset`);
    if (!selected) return;
    
    if (!confirm(`Are you sure you want to regenerate the ${assetType} asset? This will reset the video review status.`)) {
      return;
    }

    try {
      this.log(`Regenerating asset ${assetType}...`, 'info');
      const res = await fetch(`/videos/${selected.id}/assets/${assetType}/regenerate`, {
        method: 'POST'
      });
      
      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to regenerate asset');
      }
      
      this.log(`Asset ${assetType} regenerated successfully`, 'success');
      await this.refreshAfterMutation({ reloadAssets: true, reloadCalendar: true, reloadAudit: true, keepComplianceReport: true });
      const staleNotice = document.getElementById('complianceStaleNotice');
      if (staleNotice) staleNotice.style.display = 'inline';
    } catch (err) {
      this.log(`Error regenerating asset: ${err.message}`, 'error');
    }
  },


  async loadGlobalAudit() {
    const container = document.getElementById('globalAuditList');
    const auditPageContainer = document.getElementById('globalAuditListAudit');
    if (!container && !auditPageContainer) return;

    try {
      const res = await fetch('/audit?limit=25');
      if (!res.ok) throw new Error('Failed to load audit history');
      const events = await res.json();
      this.state.auditEvents = events;
      this.renderGlobalAudit();
    } catch (err) {
      if (container) container.innerHTML = `<div class="empty-state">Failed to load history.</div>`;
      if (auditPageContainer) auditPageContainer.innerHTML = `<div class="empty-state">Failed to load history.</div>`;
      this.log(`Audit history failed: ${err.message}`, 'error');
    }
  },

  renderGlobalAudit() {
    const container = document.getElementById('globalAuditList');
    const auditPageContainer = document.getElementById('globalAuditListAudit');
    if (!container && !auditPageContainer) return;

    if (!this.state.auditEvents || this.state.auditEvents.length === 0) {
      if (container) container.innerHTML = `<div class="empty-state">No operator history yet.</div>`;
      if (auditPageContainer) auditPageContainer.innerHTML = `<div class="empty-state">No operator history yet.</div>`;
      return;
    }

    const html = this.state.auditEvents.map(event => {
      const clickable = event.video_id ? `onclick="app.selectVideo(${event.video_id})"` : '';
      return `
        <div class="audit-event ${event.video_id ? 'clickable' : ''}" ${clickable}>
          <div class="audit-event-top">
            <span class="audit-badge">${this.escapeHtml(event.event_type)}</span>
            <span class="audit-time">${this.formatTime(event.created_at)}</span>
          </div>
          <div class="audit-message">${this.escapeHtml(event.message)}</div>
        </div>
      `;
    }).join('');
    if (container) container.innerHTML = html;
    if (auditPageContainer) auditPageContainer.innerHTML = html;
  },

  async loadVideoAudit() {
    const container = document.getElementById('videoAuditList');
    if (!container || !this.state.selectedVideoId) return;

    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}/audit`);
      if (!res.ok) throw new Error('Failed to load video audit trail');
      const events = await res.json();
      this.state.videoAuditEvents = events;
      this.renderVideoAudit();
    } catch (err) {
      container.innerHTML = `<div class="empty-state">Failed to load audit trail.</div>`;
      this.log(`Video audit failed: ${err.message}`, 'error');
    }
  },

  renderVideoAudit() {
    const container = document.getElementById('videoAuditList');
    if (!container) return;

    if (!this.state.videoAuditEvents || this.state.videoAuditEvents.length === 0) {
      container.innerHTML = `<div class="empty-state">No audit events for this video yet.</div>`;
      return;
    }

    container.innerHTML = this.state.videoAuditEvents.map(event => `
      <div class="audit-event">
        <div class="audit-event-top">
          <span class="audit-badge">${this.escapeHtml(event.event_type)}</span>
          <span class="audit-time">${this.formatTime(event.created_at)}</span>
        </div>
        <div class="audit-message">${this.escapeHtml(event.message)}</div>
      </div>
    `).join('');
  },

  async refreshAuditPanels() {
    await this.loadGlobalAudit();
    await this.loadVideoAudit();
  },

  async openExportModal() {
    const selected = this.requireSelectedVideo('export operator summary');
    if (!selected) return;

    const preview = document.getElementById('operatorExportPreview');
    preview.textContent = 'Loading operator summary...';
    document.getElementById('operatorExportModal').classList.remove('hidden');

    try {
      const res = await fetch(`/videos/${selected.id}/operator-export`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to export operator summary');
      }

      this.state.operatorExport = data;
      preview.textContent = JSON.stringify(data, null, 2);
      this.log('Loaded local operator export summary', 'success');
      await this.refreshAuditPanels();
    } catch (err) {
      preview.textContent = `Export failed: ${err.message}`;
      this.log(`Operator export failed: ${err.message}`, 'error');
    }
  },

  closeExportModal() {
    document.getElementById('operatorExportModal').classList.add('hidden');
  },

  async copyOperatorExport() {
    const text = document.getElementById('operatorExportPreview').textContent;
    await this.copyText(text, 'Operator export JSON copied');
  },

  downloadOperatorExport() {
    const text = document.getElementById('operatorExportPreview').textContent || '{}';
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const videoId = this.state.selectedVideoId || 'video';
    const a = document.createElement('a');
    a.href = url;
    a.download = `operator-summary-${videoId}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    this.log('Downloaded local operator export JSON', 'success');
  },

  async copyText(text, successMessage = 'Copied') {
    try {
      if (!navigator?.clipboard?.writeText) {
        throw new Error('Clipboard API unavailable in this browser context');
      }
      await navigator.clipboard.writeText(text);
      this.log(successMessage, 'success');
    } catch (err) {
      this.log(`Copy failed: ${err.message}`, 'error');
    }
  },

  formatTime(value) {
    if (!value) return '';
    try {
      return new Date(value).toLocaleString();
    } catch (_) {
      return value;
    }
  },

  escapeHtml(unsafe) {
    if (!unsafe) return '';
    return unsafe
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
  }
};

document.addEventListener('DOMContentLoaded', () => {
  app.init();
});
