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
    previewStatusByVideoId: {},
    packageDirsByVideoId: {},
    lastComplianceReportByVideoId: {},
    opportunities: [],
    agents: [],
    briefs: [],
    producerRecommendation: null,
    producerHistory: [],
    researchRuns: [],
    researchStrategies: [],
    selectedResearchRunId: null,
    selectedResearchRunDetail: null,
    commandCenterToday: null,
    pipelineDaily: null,
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
    await this.loadOpportunities();
    await this.loadAgents();
    await this.loadBriefs();
    await this.loadExecutiveProducerRecommendation();
    await this.loadProducerHistory();
    await this.loadResearchData();
    await this.loadCommandCenter();
    await this.loadPipelineDaily();
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
      opportunities: 'Opportunities',
      agents: 'Agents',
      producer: 'Producer',
      research: 'Research',
      briefs: 'Briefs',
      pipeline: 'Pipeline',
      content: 'Content',
      assets: 'Assets',
      publishing: 'Publishing',
      compliance: 'Compliance',
      audit: 'Audit'
    };
    const topNavTitle = document.getElementById('topNavTitle');
    if (topNavTitle) topNavTitle.textContent = titleMap[page] || 'Dashboard';
    if (page === 'opportunities') {
      this.loadOpportunities();
    }
    if (page === 'agents') {
      this.loadAgents();
    }
    if (page === 'producer') {
      this.loadExecutiveProducerRecommendation();
      this.loadProducerHistory();
      this.loadResearchData();
    }
    if (page === 'research') {
      this.loadResearchData();
    }
    if (page === 'briefs') {
      this.loadBriefs();
    }
    if (page === 'pipeline') {
      this.loadPipelineDaily();
    }
    if (page === 'dashboard') {
      this.loadCommandCenter();
      this.loadPipelineDaily();
    }
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

  async loadCommandCenter() {
    try {
      const res = await fetch('/command-center/today');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load command center summary');
      }
      this.state.commandCenterToday = data;
      this.renderCommandCenter(data);
      return data;
    } catch (err) {
      this.log(`Command center load failed: ${err.message}`, 'error');
      this.renderCommandCenter(null);
      return null;
    }
  },

  async loadPipelineDaily() {
    try {
      const res = await fetch('/pipeline/daily');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load daily pipeline');
      }
      this.state.pipelineDaily = data;
      this.renderDashboardPipelineSummary(data);
      this.renderPipelineDaily(data);
      return data;
    } catch (err) {
      this.log(`Pipeline load failed: ${err.message}`, 'error');
      this.renderDashboardPipelineSummary(null);
      this.renderPipelineDaily(null);
      return null;
    }
  },

  executePipelineAction(action) {
    if (!action) return;
    if (action.video_id) {
      this.selectVideo(action.video_id, { fetchAssets: true, clearCompliance: false });
    }
    const targetPage = action.target_page || 'dashboard';
    this.setActivePage(targetPage);
  },

  renderDashboardPipelineSummary(data) {
    const container = document.getElementById('dashboardPipelineSummaryList');
    if (!container) return;
    if (!data || !data.summary_counts) {
      container.innerHTML = '<div class="flow-item pending">• Pipeline summary unavailable.</div>';
      return;
    }
    const counts = data.summary_counts;
    const lines = [
      `Opportunities to review: ${counts.opportunities_to_review || 0}`,
      `Briefs to review: ${counts.briefs_to_review || 0}`,
      `Approved briefs ready to promote: ${counts.approved_briefs_ready_to_promote || 0}`,
      `Videos needing preview review: ${counts.videos_needing_preview_review || 0}`,
      `Videos needing compliance/manual approval: ${(counts.videos_needing_compliance || 0) + (counts.videos_needing_manual_approval || 0)}`,
      `Ready to package/payload: ${(counts.videos_ready_to_package || 0) + (counts.videos_ready_for_payload || 0)}`
    ];
    container.innerHTML = lines.map(line => `<div class="flow-item pending">• ${this.escapeHtml(line)}</div>`).join('');
  },

  renderPipelineDaily(data) {
    const nextText = document.getElementById('pipelineNextStepText');
    const nextButton = document.getElementById('pipelineNextStepButton');
    const stagesList = document.getElementById('pipelineStagesList');
    if (!nextText || !nextButton || !stagesList) return;

    if (!data) {
      nextText.textContent = 'Unable to load pipeline state.';
      nextButton.textContent = 'No Action';
      nextButton.disabled = true;
      nextButton.onclick = null;
      stagesList.innerHTML = '<div class="empty-state">Pipeline data unavailable.</div>';
      return;
    }

    const nextStep = data.next_step || {};
    nextText.textContent = nextStep.label ? `${nextStep.label} — ${nextStep.reason || ''}` : 'No urgent actions.';
    nextButton.textContent = nextStep.label || 'No Action';
    nextButton.disabled = !nextStep.label;
    nextButton.onclick = () => this.executePipelineAction(nextStep);

    const stageDefs = [
      {
        key: 'opportunities_to_review',
        title: '1. Opportunities to Review',
        targetPage: 'opportunities',
        buttonLabel: 'Open Opportunities'
      },
      {
        key: 'producer_recommendations',
        title: '2. Producer Recommendations',
        targetPage: 'producer',
        buttonLabel: 'Open Producer'
      },
      {
        key: 'briefs_to_review',
        title: '3. Briefs to Review',
        targetPage: 'briefs',
        buttonLabel: 'Open Briefs'
      },
      {
        key: 'approved_briefs_ready_to_promote',
        title: '4. Approved Briefs Ready to Promote',
        targetPage: 'briefs',
        buttonLabel: 'Promote Briefs'
      },
      {
        key: 'videos_needing_assets',
        title: '5. Videos Needing Assets',
        targetPage: 'assets',
        buttonLabel: 'Open Assets'
      },
      {
        key: 'videos_needing_preview',
        title: '6. Videos Needing Preview Render',
        targetPage: 'assets',
        buttonLabel: 'Render Preview'
      },
      {
        key: 'videos_needing_preview_review',
        title: '7. Videos Needing Preview Review',
        targetPage: 'assets',
        buttonLabel: 'Review Preview'
      },
      {
        key: 'videos_needing_compliance',
        title: '8. Videos Needing Compliance',
        targetPage: 'compliance',
        buttonLabel: 'Run Compliance'
      },
      {
        key: 'videos_needing_manual_approval',
        title: '9. Videos Needing Manual Approval',
        targetPage: 'compliance',
        buttonLabel: 'Manual Review'
      },
      {
        key: 'videos_ready_to_package',
        title: '10. Videos Ready to Package',
        targetPage: 'assets',
        buttonLabel: 'Package Video'
      },
      {
        key: 'videos_ready_for_payload',
        title: '11. Videos Ready for Payload',
        targetPage: 'publishing',
        buttonLabel: 'Prepare Payload'
      },
      {
        key: 'completed_payloads',
        title: '12. Completed Payloads',
        targetPage: 'audit',
        buttonLabel: 'Open Audit'
      }
    ];

    stagesList.innerHTML = stageDefs.map(stage => this.renderPipelineStage(stage, data[stage.key])).join('');
    stageDefs.forEach(stage => {
      const btn = document.getElementById(`pipelineStageBtn-${stage.key}`);
      if (!btn) return;
      btn.onclick = async () => {
        const rows = Array.isArray(data[stage.key]) ? data[stage.key] : [];
        const top = rows[0];
        if (top?.video_id) {
          await this.selectVideo(top.video_id, { fetchAssets: true, clearCompliance: false });
        }
        this.setActivePage(stage.targetPage);
      };
    });
  },

  renderPipelineStage(stage, items) {
    const rows = Array.isArray(items) ? items : [];
    const sampleRows = rows.slice(0, 3).map(item => {
      const title = item.title || item.topic || item.recommended_topic || `Item #${item.id || item.video_id || item.brief_id}`;
      const metaParts = [];
      if (item.workflow_status) metaParts.push(`Status: ${item.workflow_status}`);
      if (item.review_status) metaParts.push(`Review: ${item.review_status}`);
      if (item.status && !item.workflow_status) metaParts.push(`Status: ${item.status}`);
      if (item.total_score !== undefined) metaParts.push(`Score: ${item.total_score}`);
      if (item.confidence_label) metaParts.push(`Confidence: ${item.confidence_label}`);
      if (item.assigned_agent) metaParts.push(`Agent: ${item.assigned_agent}`);
      const metaLine = metaParts.join(' • ');
      return `
        <div class="pipeline-stage-item">
          <div class="pipeline-stage-item-title">${this.escapeHtml(title)}</div>
          <div class="pipeline-stage-item-meta">${this.escapeHtml(metaLine || '—')}</div>
          ${item.reason ? `<div class="pipeline-stage-item-reason">${this.escapeHtml(item.reason)}</div>` : ''}
        </div>
      `;
    }).join('');

    return `
      <article class="pipeline-stage">
        <div class="pipeline-stage-header">
          <div class="pipeline-stage-title">${this.escapeHtml(stage.title)}</div>
          <span class="pipeline-stage-count">${rows.length}</span>
        </div>
        <div class="pipeline-stage-items">
          ${sampleRows || '<div class="empty-state">No items in this stage.</div>'}
        </div>
        <button class="btn" id="pipelineStageBtn-${this.escapeHtml(stage.key)}">${this.escapeHtml(stage.buttonLabel)}</button>
      </article>
    `;
  },

  renderTaskList(containerId, items, emptyMessage, defaultTargetPage = 'assets') {
    const container = document.getElementById(containerId);
    if (!container) return;
    const rows = Array.isArray(items) ? items : [];
    if (rows.length === 0) {
      container.innerHTML = `<div class="empty-state">${this.escapeHtml(emptyMessage)}</div>`;
      return;
    }
    container.innerHTML = rows.map(item => `
      <div class="video-card" style="padding:0.75rem;">
        <div class="video-header">
          <div class="video-title">${this.escapeHtml(item.title || `Video #${item.video_id}`)}</div>
          <div class="video-status ${this.escapeHtml(item.workflow_status || 'idea')}">${this.escapeHtml(item.workflow_status || 'idea')}</div>
        </div>
        <div style="font-size:0.82rem; color: var(--textSecondary); margin-top:0.35rem;">${this.escapeHtml(item.reason || '')}</div>
        <button class="btn warning" style="margin-top:0.55rem;" onclick="app.openCommandCenterTask(${Number(item.video_id)}, '${this.escapeHtml(item.target_page || defaultTargetPage)}')">Open</button>
      </div>
    `).join('');
  },

  openCommandCenterTask(videoId, targetPage) {
    const page = targetPage || 'assets';
    if (videoId && Number.isFinite(videoId)) {
      this.selectVideo(Number(videoId), { fetchAssets: true, clearCompliance: false });
    }
    this.setActivePage(page);
  },

  executeCommandCenterAction(action) {
    if (!action) return;
    const targetPage = action.target_page || 'dashboard';
    if (action.video_id) {
      this.selectVideo(action.video_id, { fetchAssets: true, clearCompliance: false });
    }
    if (action.opportunity_id && targetPage === 'opportunities') {
      this.setActivePage('opportunities');
      this.log(`Focused opportunity #${action.opportunity_id} from command center.`, 'info');
      return;
    }
    this.setActivePage(targetPage);
  },

  renderCommandCenter(data) {
    const nextText = document.getElementById('nextBestActionText');
    const nextButton = document.getElementById('nextBestActionButton');
    const bestOppEl = document.getElementById('ccBestOpportunity');
    const assignedAgentEl = document.getElementById('ccAssignedAgent');
    const producerReasonEl = document.getElementById('ccProducerReason');
    const blockersText = document.getElementById('blockersNextStep');
    const checklistEl = document.getElementById('dailyChecklistList');
    const recentActivityEl = document.getElementById('commandCenterRecentActivityList');
    const briefReviewEl = document.getElementById('commandCenterBriefsReviewList');
    const briefApprovedEl = document.getElementById('commandCenterBriefsApprovedList');
    const researchThesisEl = document.getElementById('dashboardResearchThesis');
    const researchPatternEl = document.getElementById('dashboardResearchPattern');
    const researchActionEl = document.getElementById('dashboardResearchAction');
    const researchActionBtn = document.getElementById('dashboardResearchActionBtn');

    if (!nextText || !nextButton || !bestOppEl || !assignedAgentEl || !producerReasonEl || !blockersText || !checklistEl || !recentActivityEl || !briefReviewEl || !briefApprovedEl || !researchThesisEl || !researchPatternEl || !researchActionEl || !researchActionBtn) {
      return;
    }

    if (!data) {
      nextText.textContent = 'Unable to load command center summary.';
      nextButton.disabled = true;
      nextButton.textContent = 'No Action';
      nextButton.onclick = null;
      bestOppEl.textContent = '-';
      assignedAgentEl.textContent = '-';
      producerReasonEl.textContent = '-';
      blockersText.textContent = 'Command center data unavailable.';
      checklistEl.innerHTML = '<div class="flow-item pending">• Retry command center refresh.</div>';
      this.renderTaskList('commandCenterBlockersList', [], 'No blockers loaded.');
      this.renderTaskList('needsAttentionQueueList', [], 'No review queue loaded.');
      this.renderTaskList('commandCenterPackagingList', [], 'No packaging queue loaded.');
      this.renderTaskList('commandCenterPayloadList', [], 'No payload queue loaded.', 'publishing');
      briefReviewEl.innerHTML = '<div class="empty-state">No briefs pending review.</div>';
      briefApprovedEl.innerHTML = '<div class="empty-state">No approved briefs ready to promote.</div>';
      recentActivityEl.innerHTML = '<div class="empty-state">Unable to load recent activity.</div>';
      researchThesisEl.textContent = 'No research data available.';
      researchPatternEl.textContent = '-';
      researchActionEl.textContent = 'Run supervisor research when API setup is complete.';
      researchActionBtn.textContent = 'Go To Research';
      researchActionBtn.onclick = () => this.setActivePage('research');
      return;
    }

    const nextAction = data.next_best_action || {};
    nextText.textContent = nextAction.label || 'No urgent actions.';
    nextButton.disabled = !nextAction.cta_label;
    nextButton.textContent = nextAction.cta_label || 'No Action';
    nextButton.onclick = () => this.executeCommandCenterAction(nextAction);

    const bestOpportunity = data.best_opportunity;
    bestOppEl.textContent = bestOpportunity
      ? `${bestOpportunity.topic} (${bestOpportunity.score?.total_score ?? '-'} / 40)`
      : 'No opportunity selected yet.';

    const assignedAgent = data.assigned_agent;
    assignedAgentEl.textContent = assignedAgent
      ? `${assignedAgent.name} (${assignedAgent.lane || 'lane n/a'})`
      : '-';

    producerReasonEl.textContent = data.executive_recommendation?.why_make_today || data.summary_message || '-';
    const researchSummary = data.latest_research_strategy;
    if (researchSummary) {
      researchThesisEl.textContent = researchSummary.trend_thesis || 'Research strategy available.';
      researchPatternEl.textContent = researchSummary.top_pattern || '-';
      researchActionEl.textContent = researchSummary.recommended_next_action || 'Review strategy and create opportunities.';
      researchActionBtn.textContent = 'Open Research';
      researchActionBtn.onclick = () => this.setActivePage('research');
    } else {
      researchThesisEl.textContent = 'No research strategy yet.';
      researchPatternEl.textContent = '-';
      researchActionEl.textContent = 'Run supervisor research to generate original strategy directions.';
      researchActionBtn.textContent = 'Go To Research';
      researchActionBtn.onclick = () => this.setActivePage('research');
    }

    const blockers = Array.isArray(data.blockers) ? data.blockers : [];
    blockersText.textContent = blockers.length > 0
      ? `${blockers.length} blocker(s) require attention before advancing.`
      : 'No current blockers.';

    const checklistItems = Array.isArray(data.operator_checklist) ? data.operator_checklist : [];
    checklistEl.innerHTML = checklistItems.length > 0
      ? checklistItems.map(line => `<div class="flow-item pending">• ${this.escapeHtml(line)}</div>`).join('')
      : '<div class="flow-item done">✅ No checklist items.</div>';

    const needsCompliance = Array.isArray(data.needs_compliance_review) ? data.needs_compliance_review : [];
    const needsPreview = Array.isArray(data.needs_preview_review) ? data.needs_preview_review : [];
    this.renderTaskList('commandCenterBlockersList', blockers, 'No blockers right now.');
    this.renderTaskList('needsAttentionQueueList', [...needsCompliance, ...needsPreview], 'No review items right now.');
    this.renderTaskList('commandCenterPackagingList', data.ready_for_packaging || [], 'No videos ready for packaging.');
    this.renderTaskList('commandCenterPayloadList', data.ready_for_payload || [], 'No videos ready for payload.', 'publishing');
    const briefsReview = Array.isArray(data.briefs_needing_review) ? data.briefs_needing_review : [];
    const briefsApproved = Array.isArray(data.approved_briefs_ready_to_promote) ? data.approved_briefs_ready_to_promote : [];
    briefReviewEl.innerHTML = briefsReview.length > 0
      ? briefsReview.map(item => `
        <div class="video-card" style="padding:0.65rem;">
          <div class="video-title">${this.escapeHtml(item.title || item.topic || `Brief #${item.brief_id}`)}</div>
          <div class="video-meta-line">${this.escapeHtml(item.agent_name || 'Unassigned')} • ${this.escapeHtml(item.status || 'draft')}</div>
          <button class="btn warning" style="margin-top:0.45rem;" onclick="app.setActivePage('briefs')">Review Briefs</button>
        </div>
      `).join('')
      : '<div class="empty-state">No briefs pending review.</div>';
    briefApprovedEl.innerHTML = briefsApproved.length > 0
      ? briefsApproved.map(item => `
        <div class="video-card" style="padding:0.65rem;">
          <div class="video-title">${this.escapeHtml(item.title || item.topic || `Brief #${item.brief_id}`)}</div>
          <div class="video-meta-line">${this.escapeHtml(item.agent_name || 'Unassigned')} • approved</div>
          <button class="btn success" style="margin-top:0.45rem;" onclick="app.setActivePage('briefs')">Promote Briefs</button>
        </div>
      `).join('')
      : '<div class="empty-state">No approved briefs ready to promote.</div>';

    const recentEvents = Array.isArray(data.recent_audit_events) ? data.recent_audit_events : [];
    if (recentEvents.length === 0) {
      recentActivityEl.innerHTML = '<div class="empty-state">No recent activity yet.</div>';
      return;
    }
    recentActivityEl.innerHTML = recentEvents.map(event => `
      <div class="audit-event ${event.video_id ? 'clickable' : ''}" ${event.video_id ? `onclick="app.selectVideo(${event.video_id})"` : ''}>
        <div class="audit-event-top">
          <span class="audit-badge">${this.escapeHtml(event.event_type || '')}</span>
          <span class="audit-time">${this.formatTime(event.created_at)}</span>
        </div>
        <div class="audit-message">${this.escapeHtml(event.message || '')}</div>
      </div>
    `).join('');
  },

  renderGuidedFlow(summary) {
    const queue = summary.action_queue || [];
    const selectedVideo = this.getSelectedVideo();
    const selectedReadiness = selectedVideo ? this.state.readinessByVideoId[selectedVideo.id] : null;
    const draftCandidate = this.state.videos.find(video => ['idea', 'drafted'].includes(video.status) && !video.approved);
    const reviewCandidate = this.state.videos.find(video => ['needs_review', 'rejected'].includes(video.status) && !video.approved);
    const previewMissingCandidate = this.state.videos.find(video => video.approved && !video.rendered_preview_path);
    const previewUnreviewedCandidate = this.state.videos.find(video => video.approved && !!video.rendered_preview_path && !video.preview_reviewed);
    const approvedCandidate = this.state.videos.find(video => video.status === 'approved' && video.approved && !!video.rendered_preview_path && !!video.preview_reviewed);
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
    } else if (previewMissingCandidate) {
      nextAction = {
        text: `${previewMissingCandidate.title}: preview not available yet.`,
        button: 'Render Draft Preview',
        handler: async () => {
          await this.selectVideo(previewMissingCandidate.id, { fetchAssets: true, clearCompliance: false });
          this.setActivePage('assets');
          this.focusPreviewPanel();
        }
      };
    } else if (previewUnreviewedCandidate) {
      nextAction = {
        text: `${previewUnreviewedCandidate.title}: watch draft preview and mark it reviewed.`,
        button: 'Watch Draft Preview',
        handler: async () => {
          await this.selectVideo(previewUnreviewedCandidate.id, { fetchAssets: true, clearCompliance: false });
          this.setActivePage('assets');
          this.focusPreviewPanel();
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
        { label: 'Render draft preview for approved videos', done: !previewMissingCandidate },
        { label: 'Watch and review draft previews', done: !previewUnreviewedCandidate },
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
      if (nextAction.includes('preview') || nextAction.includes('watch')) {
        this.focusPreviewPanel();
      }
    }
  },

  focusPreviewPanel() {
    const panel = document.getElementById('previewPanel');
    if (!panel) return;
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

  getAgentById(agentId) {
    if (!agentId) return null;
    return (this.state.agents || []).find(agent => agent.id === agentId) || null;
  },

  async loadAgents() {
    const container = document.getElementById('agentsList');
    if (!container) return;
    try {
      const res = await fetch('/agents');
      const data = await res.json().catch(() => ([]));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load agents');
      }
      this.state.agents = Array.isArray(data) ? data : [];
      this.renderAgents();
      this.refreshResearchAgentSelect();
      this.renderOpportunities();
      this.renderExecutiveProducerRecommendation(this.state.producerRecommendation);
    } catch (err) {
      this.log(`Error loading agents: ${err.message}`, 'error');
      container.innerHTML = '<div class="empty-state">Failed to load agents.</div>';
    }
  },

  renderAgents() {
    const container = document.getElementById('agentsList');
    if (!container) return;
    const agents = this.state.agents || [];
    if (agents.length === 0) {
      container.innerHTML = '<div class="empty-state">No agents found.</div>';
      return;
    }

    container.innerHTML = agents.map(agent => `
      <article class="agent-card">
        <div class="agent-card-header">
          <div>
            <div class="video-title-main">${this.escapeHtml(agent.name || '-')}</div>
            <div class="video-meta-line">${this.escapeHtml(agent.lane || '-')}</div>
          </div>
          <span class="video-status ${agent.is_active ? 'approved' : 'blocked'}">${agent.is_active ? 'Active' : 'Inactive'}</span>
        </div>
        <div class="agent-edit-grid">
          <label class="agent-field">
            <span>Focus</span>
            <textarea class="opportunity-input agent-textarea" id="agentFocus-${agent.id}" rows="3" placeholder="Agent focus">${this.escapeHtml(agent.focus || '')}</textarea>
          </label>
          <label class="agent-field">
            <span>Monetization Focus</span>
            <textarea class="opportunity-input agent-textarea" id="agentMonetization-${agent.id}" rows="3" placeholder="Monetization focus">${this.escapeHtml(agent.monetization_focus || '')}</textarea>
          </label>
          <label class="agent-field">
            <span>Compliance Notes</span>
            <textarea class="opportunity-input agent-textarea" id="agentCompliance-${agent.id}" rows="4" placeholder="Compliance notes">${this.escapeHtml(agent.compliance_notes || '')}</textarea>
          </label>
          <label class="agent-field">
            <span>Production Rules</span>
            <textarea class="opportunity-input agent-textarea" id="agentRules-${agent.id}" rows="4" placeholder="Production rules">${this.escapeHtml(agent.production_rules || '')}</textarea>
          </label>
        </div>
        <div class="row-actions agent-actions">
          <button class="btn success" onclick="app.saveAgent(${agent.id})">Save</button>
          <button class="btn" onclick="app.toggleAgent(${agent.id}, ${agent.is_active ? 'false' : 'true'})">${agent.is_active ? 'Set Inactive' : 'Set Active'}</button>
        </div>
      </article>
    `).join('');
  },

  async saveAgent(agentId) {
    const focus = document.getElementById(`agentFocus-${agentId}`)?.value?.trim();
    const monetizationFocus = document.getElementById(`agentMonetization-${agentId}`)?.value?.trim();
    const complianceNotes = document.getElementById(`agentCompliance-${agentId}`)?.value?.trim();
    const productionRules = document.getElementById(`agentRules-${agentId}`)?.value?.trim();
    try {
      const res = await fetch(`/agents/${agentId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          focus: focus ?? null,
          monetization_focus: monetizationFocus ?? null,
          compliance_notes: complianceNotes ?? null,
          production_rules: productionRules ?? null
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to update agent');
      }
      this.log(`Updated agent profile: ${data.name}`, 'success');
      await this.loadAgents();
      await this.loadGlobalAudit();
    } catch (err) {
      this.log(`Agent update failed: ${err.message}`, 'error');
    }
  },

  async toggleAgent(agentId, isActive) {
    try {
      const res = await fetch(`/agents/${agentId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !!isActive })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to update agent status');
      }
      this.log(`Agent ${data.name} is now ${data.is_active ? 'active' : 'inactive'}.`, 'success');
      await this.loadAgents();
      await this.loadOpportunities();
      await this.loadGlobalAudit();
    } catch (err) {
      this.log(`Agent status update failed: ${err.message}`, 'error');
    }
  },

  async loadOpportunities() {
    const tableBody = document.getElementById('opportunityList');
    if (!tableBody) return;
    try {
      const res = await fetch('/opportunities/review-queue?limit=200');
      const data = await res.json().catch(() => ([]));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load opportunities');
      }
      this.state.opportunities = Array.isArray(data) ? data : [];
      this.renderOpportunities();
    } catch (err) {
      this.log(`Error loading opportunities: ${err.message}`, 'error');
      tableBody.innerHTML = '<tr><td colspan="7" class="table-empty">Failed to load opportunities.</td></tr>';
      this.renderBestOpportunity(null);
    }
  },

  renderOpportunities() {
    const tableBody = document.getElementById('opportunityList');
    if (!tableBody) return;
    const items = this.state.opportunities || [];
    this.renderBestOpportunity(items.length > 0 ? items[0] : null);

    if (items.length === 0) {
      tableBody.innerHTML = '<tr><td colspan="7" class="table-empty">No opportunities yet.</td></tr>';
      return;
    }

    tableBody.innerHTML = items.map(item => {
      const matchedAgent = this.getAgentById(item.assigned_agent_id);
      const agentName = matchedAgent?.name || item.assigned_agent || 'Unassigned';
      const agentLane = matchedAgent?.lane || 'Lane not matched';
      return `
      <tr>
        <td>
          <div class="video-title-main">${this.escapeHtml(item.topic)}</div>
          <div class="video-meta-line">${this.escapeHtml(item.niche_lane || '—')} • ${this.escapeHtml(item.audience || '—')}</div>
          <div class="video-meta-line opportunity-agent-line">Agent: ${this.escapeHtml(agentName)}${item.assigned_agent_id ? ` (#${item.assigned_agent_id})` : ''}</div>
        </td>
        <td>
          <span class="video-status ${this.escapeHtml(item.review_status || 'unreviewed')}">${this.escapeHtml(this.formatReviewStatus(item.review_status))}</span>
        </td>
        <td><strong>${this.escapeHtml(String(item.score?.total_score ?? '-'))}</strong></td>
        <td>
          <div>${this.escapeHtml(item.expected_monetization_path || item.monetization_path || '—')}</div>
          <div class="video-meta-line">${this.escapeHtml(agentLane)}</div>
        </td>
        <td class="opportunity-decision-cell">
          <textarea class="opportunity-input" id="oppOperatorNotes-${item.id}" rows="2" placeholder="Operator notes">${this.escapeHtml(item.operator_notes || '')}</textarea>
          <textarea class="opportunity-input" id="oppDecisionSummary-${item.id}" rows="2" placeholder="Decision summary">${this.escapeHtml(item.decision_summary || '')}</textarea>
          <textarea class="opportunity-input ${item.review_status === 'rejected' ? '' : 'hidden'}" id="oppRejectionReason-${item.id}" rows="2" placeholder="Rejection reason">${this.escapeHtml(item.rejection_reason || '')}</textarea>
        </td>
        <td>${item.promoted_video_id ? `#${item.promoted_video_id}` : '—'}</td>
        <td>
          <div class="row-actions opportunity-actions">
            <button class="btn" onclick="app.scoreOpportunity(${item.id})">Score Opportunity</button>
            <button class="btn" onclick="app.updateOpportunityReview(${item.id}, 'shortlisted')">Shortlist</button>
            <button class="btn" onclick="app.updateOpportunityReview(${item.id}, 'needs_more_research')">Needs More Research</button>
            <button class="btn danger" onclick="app.updateOpportunityReview(${item.id}, 'rejected')">Reject</button>
            <button class="btn success" onclick="app.updateOpportunityReview(${item.id}, 'approved_for_video')">Approve for Video</button>
            <button class="btn" ${['shortlisted', 'approved_for_video'].includes(item.review_status) ? '' : 'disabled'} onclick="app.createBriefFromOpportunity(${item.id})">Create Brief</button>
            <button class="btn success" ${item.review_status === 'approved_for_video' ? '' : 'disabled'} onclick="app.promoteOpportunity(${item.id})">Promote to Video</button>
          </div>
          ${item.review_status === 'approved_for_video'
            ? '<div class="opportunity-help-text">Ready for promotion.</div>'
            : '<div class="opportunity-help-text">Promotion blocked: approve for video first.</div>'}
        </td>
      </tr>
    `;
    }).join('');
  },

  formatReviewStatus(status) {
    const label = (status || 'unreviewed').replaceAll('_', ' ').trim();
    return label.charAt(0).toUpperCase() + label.slice(1);
  },

  renderBestOpportunity(item) {
    const topicEl = document.getElementById('oppBestTopic');
    const scoreEl = document.getElementById('oppBestScore');
    const monetizationEl = document.getElementById('oppBestMonetization');
    const titleEl = document.getElementById('oppBestTitle');
    const thumbEl = document.getElementById('oppBestThumb');
    const ctaEl = document.getElementById('oppBestCta');
    const agentEl = document.getElementById('oppBestAgent');
    const complianceEl = document.getElementById('oppBestCompliance');
    const reviewStatusEl = document.getElementById('oppBestReviewStatus');
    const scoreGridEl = document.getElementById('oppScoreBreakdown');
    if (!topicEl || !scoreEl || !monetizationEl || !titleEl || !thumbEl || !ctaEl || !agentEl || !complianceEl || !reviewStatusEl || !scoreGridEl) {
      return;
    }

    if (!item) {
      topicEl.textContent = 'No opportunities yet';
      scoreEl.textContent = '-';
      monetizationEl.textContent = '-';
      titleEl.textContent = '-';
      thumbEl.textContent = '-';
      ctaEl.textContent = '-';
      agentEl.textContent = '-';
      complianceEl.textContent = '-';
      reviewStatusEl.textContent = 'unreviewed';
      reviewStatusEl.className = 'video-status unreviewed';
      scoreGridEl.innerHTML = '<div class="empty-state" style="height:auto; padding:0.75rem;">Add an opportunity to see scoring.</div>';
      return;
    }

    topicEl.textContent = item.topic || '-';
    scoreEl.textContent = `${item.score?.total_score ?? '-'} / 40`;
    monetizationEl.textContent = item.expected_monetization_path || item.monetization_path || '-';
    titleEl.textContent = item.recommended_title || '-';
    thumbEl.textContent = item.thumbnail_angle || '-';
    ctaEl.textContent = item.recommended_cta || '-';
    const matchedAgent = this.getAgentById(item.assigned_agent_id);
    agentEl.textContent = matchedAgent
      ? `${matchedAgent.name} (${matchedAgent.lane || 'lane n/a'})`
      : (item.assigned_agent || '-');
    complianceEl.textContent = item.compliance_risk_note || '-';
    reviewStatusEl.textContent = this.formatReviewStatus(item.review_status);
    reviewStatusEl.className = `video-status ${this.escapeHtml(item.review_status || 'unreviewed')}`;

    const score = item.score || {};
    const rows = [
      ['Search Demand', score.search_demand],
      ['Buyer Intent', score.buyer_intent],
      ['Affiliate Potential', score.affiliate_potential],
      ['Sponsorship Potential', score.sponsorship_potential],
      ['Production Difficulty (lower is better)', score.production_difficulty],
      ['Compliance Risk (lower is better)', score.compliance_risk],
      ['Trend Freshness', score.trend_freshness],
      ['Product Connection', score.product_connection],
    ];
    scoreGridEl.innerHTML = rows.map(([label, value]) => `
      <div class="score-row">
        <span>${this.escapeHtml(String(label))}</span>
        <strong>${this.escapeHtml(String(value ?? '-'))}/5</strong>
      </div>
    `).join('');
  },

  async createOpportunity() {
    const topic = document.getElementById('oppTopic')?.value?.trim() || '';
    const nicheLane = document.getElementById('oppNicheLane')?.value?.trim() || '';
    const audience = document.getElementById('oppAudience')?.value?.trim() || '';
    const monetizationPath = document.getElementById('oppMonetizationPath')?.value?.trim() || '';
    const notes = document.getElementById('oppNotes')?.value?.trim() || '';
    const btn = document.getElementById('btnCreateOpportunity');

    if (!topic || !nicheLane || !audience || !monetizationPath) {
      this.log('Topic, niche lane, audience, and monetization path are required.', 'error');
      return;
    }

    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Creating...';
    }
    try {
      const res = await fetch('/opportunities', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          channel_id: 1,
          topic,
          niche_lane: nicheLane,
          audience,
          monetization_path: monetizationPath,
          notes: notes || null
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to create opportunity');
      }
      this.log('Opportunity created and scored.', 'success');
      const fieldIds = ['oppTopic', 'oppNicheLane', 'oppAudience', 'oppMonetizationPath', 'oppNotes'];
      fieldIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      await this.loadOpportunities();
    } catch (err) {
      this.log(`Create opportunity failed: ${err.message}`, 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Create Opportunity';
      }
    }
  },

  setDailySeedStatus(message, type = 'info') {
    const el = document.getElementById('dailySeedStatusMessage');
    if (!el) return;
    el.textContent = message;
    el.style.color = type === 'error' ? 'var(--danger)' : (type === 'success' ? 'var(--success)' : 'var(--textSecondary)');
  },

  async generateDailyOpportunities(limit = 7) {
    const safeLimit = Number.isFinite(limit) ? Math.max(1, Math.min(50, Number(limit))) : 7;
    this.setDailySeedStatus('Generating deterministic daily opportunity batch...', 'info');
    try {
      const res = await fetch('/opportunities/intake/daily-seed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit: safeLimit })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to generate daily opportunities');
      }
      const summary = `Created ${data.created_count} opportunity(ies), skipped ${data.skipped_duplicates} duplicate(s).`;
      this.setDailySeedStatus(`${summary} Next step: review, shortlist, and approve the strongest opportunities.`, 'success');
      this.log(`Daily seed complete. ${summary}`, 'success');
      await this.loadOpportunities();
      await this.loadCommandCenter();
      await this.loadPipelineDaily();
      await this.loadExecutiveProducerRecommendation();
      await this.loadProducerHistory();
    } catch (err) {
      this.setDailySeedStatus(`Daily seed failed: ${err.message}`, 'error');
      this.log(`Daily seed failed: ${err.message}`, 'error');
    }
  },

  async scoreOpportunity(opportunityId) {
    try {
      const res = await fetch(`/opportunities/${opportunityId}/score`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to score opportunity');
      }
      this.log(`Opportunity rescored: ${data.topic}`, 'success');
      await this.loadOpportunities();
    } catch (err) {
      this.log(`Score opportunity failed: ${err.message}`, 'error');
    }
  },

  async updateOpportunityReview(opportunityId, reviewStatus) {
    const item = (this.state.opportunities || []).find(opportunity => opportunity.id === opportunityId);
    if (!item) {
      this.log('Opportunity not found in current queue.', 'error');
      return;
    }

    const operatorNotes = document.getElementById(`oppOperatorNotes-${opportunityId}`)?.value?.trim() || null;
    const decisionSummary = document.getElementById(`oppDecisionSummary-${opportunityId}`)?.value?.trim() || null;
    const rejectionReasonEl = document.getElementById(`oppRejectionReason-${opportunityId}`);
    const rejectionReason = rejectionReasonEl?.value?.trim() || null;

    if (rejectionReasonEl) {
      rejectionReasonEl.classList.toggle('hidden', reviewStatus !== 'rejected');
    }

    try {
      const res = await fetch(`/opportunities/${opportunityId}/review`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          review_status: reviewStatus,
          operator_notes: operatorNotes,
          decision_summary: decisionSummary,
          rejection_reason: reviewStatus === 'rejected' ? rejectionReason : null
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to update opportunity review');
      }
      this.log(`Opportunity review updated: ${data.topic} -> ${this.formatReviewStatus(data.review_status)}.`, 'success');
      await this.loadOpportunities();
    } catch (err) {
      this.log(`Review update failed: ${err.message}`, 'error');
    }
  },

  async promoteOpportunity(opportunityId) {
    const item = (this.state.opportunities || []).find(opportunity => opportunity.id === opportunityId);
    if (item && item.review_status !== 'approved_for_video') {
      this.log('Promotion blocked: opportunity must be approved for video first.', 'error');
      return;
    }
    try {
      const res = await fetch(`/opportunities/${opportunityId}/promote-to-video`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to promote opportunity');
      }
      this.log(`Promoted opportunity to video idea: ${data.title}`, 'success');
      this.state.selectedVideoId = data.id;
      await this.refreshAfterMutation({
        reloadAssets: false,
        reloadCalendar: true,
        reloadAudit: true,
        keepComplianceReport: false,
        reloadOpportunities: true
      });
      this.setActivePage('content');
    } catch (err) {
      this.log(`Promote to video failed: ${err.message}`, 'error');
    }
  },

  async createBriefFromOpportunity(opportunityId) {
    try {
      const res = await fetch(`/production-briefs/from-opportunity/${opportunityId}`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to create production brief');
      }
      this.log(data.message || `Created production brief #${data.brief?.id}.`, 'success');
      await this.loadBriefs();
      await this.loadCommandCenter();
      this.setActivePage('briefs');
    } catch (err) {
      this.log(`Create brief failed: ${err.message}`, 'error');
    }
  },

  async loadBriefs() {
    const container = document.getElementById('briefsList');
    if (!container) return;
    try {
      const res = await fetch('/production-briefs');
      const data = await res.json().catch(() => ([]));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load production briefs');
      }
      this.state.briefs = Array.isArray(data) ? data : [];
      this.renderBriefs();
      this.renderExecutiveProducerRecommendation(this.state.producerRecommendation);
    } catch (err) {
      this.log(`Error loading briefs: ${err.message}`, 'error');
      container.innerHTML = '<div class="empty-state">Failed to load production briefs.</div>';
    }
  },

  renderBriefs() {
    const container = document.getElementById('briefsList');
    if (!container) return;
    const briefs = this.state.briefs || [];
    if (briefs.length === 0) {
      container.innerHTML = '<div class="empty-state">No production briefs yet. Create one from Opportunities or Producer.</div>';
      return;
    }

    container.innerHTML = briefs.map(brief => {
      const agent = this.getAgentById(brief.assigned_agent_id);
      const agentText = agent ? `${agent.name} (${agent.lane || 'lane n/a'})` : 'Unassigned agent';
      const status = this.escapeHtml(brief.status || 'draft');
      const promoteDisabled = brief.status !== 'approved';
      return `
        <article class="agent-card">
          <div class="agent-card-header">
            <div>
              <div class="video-title-main">${this.escapeHtml(brief.title || brief.topic || 'Untitled brief')}</div>
              <div class="video-meta-line">${this.escapeHtml(brief.topic || '-')}</div>
            </div>
            <span class="video-status ${status}">${status}</span>
          </div>
          <div class="meta-row"><span class="meta-label">Assigned Agent</span><span class="meta-value">${this.escapeHtml(agentText)}</span></div>
          <div class="meta-row"><span class="meta-label">Lane</span><span class="meta-value">${this.escapeHtml(brief.niche_lane || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">Hook</span><span class="meta-value pre-wrap">${this.escapeHtml(brief.hook || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">Outline</span><span class="meta-value pre-wrap">${this.escapeHtml(brief.outline || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">Script Plan</span><span class="meta-value pre-wrap">${this.escapeHtml(brief.script_plan || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">B-Roll Plan</span><span class="meta-value pre-wrap">${this.escapeHtml(brief.b_roll_plan || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">CTA</span><span class="meta-value">${this.escapeHtml(brief.cta || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">Compliance Notes</span><span class="meta-value pre-wrap">${this.escapeHtml(brief.compliance_notes || '-')}</span></div>
          <div class="meta-row"><span class="meta-label">Claims To Verify</span><span class="meta-value pre-wrap">${this.escapeHtml(brief.claims_to_verify || '-')}</span></div>
          <label class="agent-field" style="margin-top:0.5rem;">
            <span>Operator Notes</span>
            <textarea class="opportunity-input agent-textarea" id="briefNotes-${brief.id}" rows="3" placeholder="Operator review notes">${this.escapeHtml(brief.operator_review_notes || '')}</textarea>
          </label>
          <div class="row-actions agent-actions">
            <button class="btn" onclick="app.updateBriefStatus(${brief.id}, 'needs_revision')">Mark Needs Revision</button>
            <button class="btn success" onclick="app.updateBriefStatus(${brief.id}, 'approved')">Approve Brief</button>
            <button class="btn" onclick="app.updateBriefStatus(${brief.id}, 'draft')">Set Draft</button>
            <button class="btn success" ${promoteDisabled ? 'disabled' : ''} onclick="app.promoteBriefToVideo(${brief.id})">Promote to Video</button>
          </div>
        </article>
      `;
    }).join('');
  },

  async updateBriefStatus(briefId, status) {
    const notes = document.getElementById(`briefNotes-${briefId}`)?.value?.trim() || null;
    try {
      const res = await fetch(`/production-briefs/${briefId}/review`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, operator_review_notes: notes })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to update brief');
      }
      this.log(`Updated brief #${briefId} -> ${status}.`, 'success');
      await this.loadBriefs();
      await this.loadCommandCenter();
      await this.loadGlobalAudit();
    } catch (err) {
      this.log(`Update brief failed: ${err.message}`, 'error');
    }
  },

  async promoteBriefToVideo(briefId) {
    try {
      const res = await fetch(`/production-briefs/${briefId}/promote-to-video`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to promote brief');
      }
      this.log(`Promoted brief to video idea: ${data.title}`, 'success');
      this.state.selectedVideoId = data.id;
      await this.refreshAfterMutation({
        reloadAssets: false,
        reloadCalendar: true,
        reloadAudit: true,
        keepComplianceReport: false,
        reloadOpportunities: true
      });
      await this.loadBriefs();
      await this.loadCommandCenter();
      this.setActivePage('content');
    } catch (err) {
      this.log(`Promote brief failed: ${err.message}`, 'error');
    }
  },

  async createBriefFromProducerRecommendation() {
    const recommendation = this.state.producerRecommendation;
    if (!recommendation || !recommendation.selected_opportunity_id) {
      this.log('No recommended opportunity available to create a brief.', 'error');
      return;
    }
    await this.createBriefFromOpportunity(recommendation.selected_opportunity_id);
  },

  async loadExecutiveProducerRecommendation() {
    try {
      const res = await fetch('/executive-producer/recommendation');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load executive producer recommendation');
      }
      this.state.producerRecommendation = data;
      this.renderExecutiveProducerRecommendation(data);
      return data;
    } catch (err) {
      this.log(`Producer recommendation load failed: ${err.message}`, 'error');
      this.renderExecutiveProducerRecommendation(null);
      return null;
    }
  },

  async runExecutiveProducer() {
    try {
      const res = await fetch('/executive-producer/recommendation/run', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to run executive producer recommendation');
      }
      this.state.producerRecommendation = data;
      this.renderExecutiveProducerRecommendation(data);
      this.log('Executive producer recommendation generated.', 'success');
      await this.loadProducerHistory();
      await this.loadPipelineDaily();
      await this.loadGlobalAudit();
    } catch (err) {
      this.log(`Run executive producer failed: ${err.message}`, 'error');
    }
  },

  renderExecutiveProducerRecommendation(data) {
    const dashboardTopic = document.getElementById('producerDashboardTopic');
    const dashboardConfidence = document.getElementById('producerDashboardConfidence');
    const dashboardAgent = document.getElementById('producerDashboardAgent');
    const dashboardMonetization = document.getElementById('producerDashboardMonetization');
    const producerTopic = document.getElementById('producerTopic');
    const producerWhy = document.getElementById('producerWhy');
    const producerNiche = document.getElementById('producerNiche');
    const producerAgent = document.getElementById('producerAgent');
    const producerAgentLane = document.getElementById('producerAgentLane');
    const producerAgentFocus = document.getElementById('producerAgentFocus');
    const producerAgentMonetization = document.getElementById('producerAgentMonetization');
    const producerAgentCompliance = document.getElementById('producerAgentCompliance');
    const producerAgentRules = document.getElementById('producerAgentRules');
    const producerTitle = document.getElementById('producerTitle');
    const producerThumb = document.getElementById('producerThumb');
    const producerCta = document.getElementById('producerCta');
    const producerMonetization = document.getElementById('producerMonetization');
    const producerConfidence = document.getElementById('producerConfidence');
    const producerRisks = document.getElementById('producerRisks');
    const producerChecklist = document.getElementById('producerChecklist');
    const producerBrief = document.getElementById('producerBrief');
    const producerEmptyState = document.getElementById('producerEmptyState');
    const viewBtn = document.getElementById('btnProducerViewOpportunity');
    const createBriefBtn = document.getElementById('btnProducerCreateBrief');
    const promoteBtn = document.getElementById('btnProducerPromote');

    if (!dashboardTopic || !dashboardConfidence || !dashboardAgent || !dashboardMonetization || !producerTopic || !producerWhy || !producerNiche || !producerAgent || !producerAgentLane || !producerAgentFocus || !producerAgentMonetization || !producerAgentCompliance || !producerAgentRules || !producerTitle || !producerThumb || !producerCta || !producerMonetization || !producerConfidence || !producerRisks || !producerChecklist || !producerBrief || !producerEmptyState || !viewBtn || !createBriefBtn || !promoteBtn) {
      return;
    }

    if (!data) {
      dashboardTopic.textContent = 'Run Executive Producer to generate a daily recommendation.';
      dashboardConfidence.textContent = 'n/a';
      dashboardConfidence.className = 'video-status';
      dashboardAgent.textContent = '-';
      dashboardMonetization.textContent = '-';
      producerTopic.textContent = 'Run Executive Producer to generate a recommendation.';
      producerWhy.textContent = '-';
      producerNiche.textContent = '-';
      producerAgent.textContent = '-';
      producerAgentLane.textContent = '-';
      producerAgentFocus.textContent = '-';
      producerAgentMonetization.textContent = '-';
      producerAgentCompliance.textContent = '-';
      producerAgentRules.textContent = '-';
      producerTitle.textContent = '-';
      producerThumb.textContent = '-';
      producerCta.textContent = '-';
      producerMonetization.textContent = '-';
      producerConfidence.textContent = 'n/a';
      producerConfidence.className = 'video-status';
      producerRisks.textContent = '-';
      producerChecklist.textContent = '-';
      producerBrief.textContent = '-';
      producerEmptyState.textContent = 'No recommendation loaded.';
      viewBtn.disabled = true;
      createBriefBtn.disabled = true;
      promoteBtn.disabled = true;
      this.renderProducerResearchStrategy(null);
      return;
    }

    const emptyState = data.empty_state_message || '';
    const hasOpportunity = !!data.selected_opportunity_id;
    const confidence = data.confidence_label || 'low';
    const confidenceLabel = confidence.charAt(0).toUpperCase() + confidence.slice(1);
    const matchedAgentName = data.matched_agent_name || data.assigned_agent || '-';
    const matchedAgentLane = data.matched_agent_lane || '-';

    dashboardTopic.textContent = hasOpportunity
      ? (data.recommended_topic || data.recommended_title || 'Best pick generated.')
      : emptyState || 'No recommendation available.';
    dashboardConfidence.textContent = confidenceLabel;
    dashboardConfidence.className = `video-status ${this.escapeHtml(confidence)}`;
    dashboardAgent.textContent = matchedAgentName === '-' ? '-' : `${matchedAgentName} (${matchedAgentLane})`;
    dashboardMonetization.textContent = data.monetization_path || '-';

    producerTopic.textContent = hasOpportunity
      ? (data.recommended_topic || 'Recommendation generated.')
      : (emptyState || 'No recommendation available.');
    producerWhy.textContent = data.why_make_today || '-';
    producerNiche.textContent = data.niche_lane || '-';
    producerAgent.textContent = matchedAgentName;
    producerAgentLane.textContent = matchedAgentLane;
    producerAgentFocus.textContent = data.matched_agent_focus || '-';
    producerAgentMonetization.textContent = data.matched_agent_monetization_focus || '-';
    producerAgentCompliance.textContent = data.matched_agent_compliance_notes || '-';
    producerAgentRules.textContent = data.matched_agent_production_rules || '-';
    producerTitle.textContent = data.recommended_title || '-';
    producerThumb.textContent = data.thumbnail_angle || '-';
    producerCta.textContent = data.recommended_cta || '-';
    producerMonetization.textContent = data.monetization_path || '-';
    producerConfidence.textContent = confidenceLabel;
    producerConfidence.className = `video-status ${this.escapeHtml(confidence)}`;
    producerRisks.textContent = data.risks_to_review || '-';
    producerChecklist.textContent = data.operator_checklist || '-';
    producerBrief.textContent = data.production_brief || '-';
    producerEmptyState.textContent = emptyState || 'Recommendation ready.';

    viewBtn.disabled = !hasOpportunity;
    createBriefBtn.disabled = !hasOpportunity;
    promoteBtn.disabled = !(hasOpportunity && data.selected_review_status === 'approved_for_video');
    this.renderProducerResearchStrategy(data);
  },

  async loadProducerHistory() {
    const container = document.getElementById('producerHistoryList');
    if (!container) return;
    try {
      const res = await fetch('/executive-producer/history?limit=25');
      const data = await res.json().catch(() => ([]));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load producer history');
      }
      this.state.producerHistory = Array.isArray(data) ? data : [];
      this.renderProducerHistory();
    } catch (err) {
      this.log(`Producer history failed: ${err.message}`, 'error');
      container.innerHTML = '<div class="empty-state">Failed to load recommendation history.</div>';
    }
  },

  renderProducerHistory() {
    const container = document.getElementById('producerHistoryList');
    if (!container) return;
    const history = this.state.producerHistory || [];
    if (history.length === 0) {
      container.innerHTML = '<div class="empty-state">No recommendation history yet.</div>';
      return;
    }
    container.innerHTML = history.map(item => `
      <div class="audit-event">
        <div class="audit-event-top">
          <span class="audit-badge">${this.escapeHtml((item.confidence_label || 'low').toUpperCase())}</span>
          <span class="audit-time">${this.formatTime(item.created_at)}</span>
        </div>
        <div class="audit-message">${this.escapeHtml(item.recommended_topic || item.empty_state_message || 'Recommendation record')}</div>
      </div>
    `).join('');
  },

  getLatestResearchStrategy() {
    const strategies = this.state.researchStrategies || [];
    return strategies.length > 0 ? strategies[0] : null;
  },

  renderProducerResearchStrategy(recommendation = null) {
    const thesisEl = document.getElementById('producerResearchThesis');
    const patternEl = document.getElementById('producerResearchPattern');
    const directionEl = document.getElementById('producerResearchDirection');
    if (!thesisEl || !patternEl || !directionEl) return;

    const strategies = this.state.researchStrategies || [];
    if (strategies.length === 0) {
      thesisEl.textContent = 'No related research strategy yet.';
      patternEl.textContent = '-';
      directionEl.textContent = 'Run research with a niche lane and query, then create opportunities from strategy.';
      return;
    }

    const recLane = (recommendation?.niche_lane || '').toLowerCase().trim();
    const matched = strategies.find(item => (item.niche_lane || '').toLowerCase().trim() === recLane) || strategies[0];
    thesisEl.textContent = matched.trend_thesis || 'Research strategy available.';
    patternEl.textContent = matched.top_pattern || '-';
    directionEl.textContent = matched.recommended_next_action || 'Review strategy and apply original angles.';
  },

  openResearchFromProducer() {
    this.setActivePage('research');
    this.log('Opened Research workspace from Producer.', 'info');
  },

  refreshResearchAgentSelect() {
    const selectEl = document.getElementById('researchAssignedAgent');
    if (!selectEl) return;
    const current = selectEl.value;
    const activeAgents = (this.state.agents || []).filter(agent => agent.is_active);
    const options = ['<option value="">Auto-match active agent</option>'];
    activeAgents.forEach(agent => {
      options.push(
        `<option value="${agent.id}">${this.escapeHtml(agent.name)}${agent.lane ? ` (${this.escapeHtml(agent.lane)})` : ''}</option>`
      );
    });
    selectEl.innerHTML = options.join('');
    if (current && activeAgents.some(agent => String(agent.id) === String(current))) {
      selectEl.value = current;
    }
  },

  async loadResearchData() {
    await this.loadResearchStrategies();
    await this.loadResearchRuns();
    this.refreshResearchAgentSelect();
    this.renderProducerResearchStrategy(this.state.producerRecommendation);
  },

  async loadResearchRuns() {
    const runList = document.getElementById('researchRunList');
    if (!runList) return;
    try {
      const res = await fetch('/research/runs?limit=30');
      const data = await res.json().catch(() => ([]));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load research runs');
      }
      this.state.researchRuns = Array.isArray(data) ? data : [];
      this.renderResearchRuns();
      const activeId = this.state.selectedResearchRunId;
      if (activeId) {
        const stillExists = this.state.researchRuns.some(item => item.id === activeId);
        if (stillExists) {
          await this.selectResearchRun(activeId);
        } else {
          this.state.selectedResearchRunId = null;
          this.state.selectedResearchRunDetail = null;
          this.renderResearchRunDetail(null);
        }
      } else if (this.state.researchRuns.length > 0) {
        await this.selectResearchRun(this.state.researchRuns[0].id);
      } else {
        this.renderResearchRunDetail(null);
      }
    } catch (err) {
      this.log(`Research runs load failed: ${err.message}`, 'error');
      runList.innerHTML = '<div class="empty-state">Failed to load research runs.</div>';
      this.renderResearchRunDetail(null);
    }
  },

  async loadResearchStrategies() {
    const container = document.getElementById('researchStrategyCards');
    if (!container) return;
    try {
      const res = await fetch('/research/strategies?limit=20');
      const data = await res.json().catch(() => ([]));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load research strategies');
      }
      this.state.researchStrategies = Array.isArray(data) ? data : [];
      this.renderResearchStrategies();
    } catch (err) {
      this.log(`Research strategies load failed: ${err.message}`, 'error');
      container.innerHTML = '<div class="empty-state">Failed to load research strategies.</div>';
    }
  },

  renderResearchRuns() {
    const container = document.getElementById('researchRunList');
    if (!container) return;
    const runs = this.state.researchRuns || [];
    if (runs.length === 0) {
      container.innerHTML = '<div class="empty-state">No research runs yet.</div>';
      return;
    }
    container.innerHTML = runs.map(run => `
      <div class="audit-event ${this.state.selectedResearchRunId === run.id ? 'clickable' : ''}" onclick="app.selectResearchRun(${run.id})">
        <div class="audit-event-top">
          <span class="audit-badge">${this.escapeHtml((run.status || 'pending').toUpperCase())}</span>
          <span class="audit-time">${this.formatTime(run.created_at)}</span>
        </div>
        <div class="audit-message">${this.escapeHtml(run.query || run.niche_lane || `Run #${run.id}`)}</div>
        <div class="video-meta-line">${this.escapeHtml(run.niche_lane || '-')} • videos: ${run.source_video_count || 0} • channels: ${run.source_channel_count || 0}</div>
        ${run.setup_required ? `<div class="opportunity-help-text">${this.escapeHtml(run.setup_message || 'Setup required')}</div>` : ''}
      </div>
    `).join('');
  },

  renderResearchStrategies() {
    const container = document.getElementById('researchStrategyCards');
    if (!container) return;
    const strategies = this.state.researchStrategies || [];
    if (strategies.length === 0) {
      container.innerHTML = '<div class="empty-state">No research strategies yet.</div>';
      return;
    }
    container.innerHTML = strategies.slice(0, 4).map(strategy => `
      <article class="agent-card">
        <div class="agent-card-header">
          <div>
            <div class="video-title-main">${this.escapeHtml(strategy.niche_lane || 'Research Strategy')}</div>
            <div class="video-meta-line">${this.escapeHtml(strategy.query || '-')}</div>
          </div>
          <span class="video-status approved">Strategy</span>
        </div>
        <div class="meta-row"><span class="meta-label">Thesis</span><span class="meta-value">${this.escapeHtml(strategy.trend_thesis || '-')}</span></div>
        <div class="meta-row"><span class="meta-label">Top Pattern</span><span class="meta-value">${this.escapeHtml(strategy.top_pattern || '-')}</span></div>
        <div class="meta-row"><span class="meta-label">Next Action</span><span class="meta-value">${this.escapeHtml(strategy.recommended_next_action || '-')}</span></div>
      </article>
    `).join('');
  },

  async runSupervisorResearch() {
    const nicheLane = document.getElementById('researchNicheLane')?.value?.trim() || '';
    const query = document.getElementById('researchQuery')?.value?.trim() || '';
    const assignedAgentRaw = document.getElementById('researchAssignedAgent')?.value || '';
    const maxResultsRaw = document.getElementById('researchMaxResults')?.value || '10';
    const maxResults = Math.max(1, Math.min(25, Number(maxResultsRaw) || 10));
    const button = document.getElementById('btnRunResearch');

    if (!nicheLane || !query) {
      this.log('Niche lane and query are required to run research.', 'error');
      return;
    }

    if (button) {
      button.classList.add('loading');
      button.disabled = true;
    }
    try {
      const payload = {
        niche_lane: nicheLane,
        query,
        max_results: maxResults,
      };
      if (assignedAgentRaw) {
        payload.assigned_agent_id = Number(assignedAgentRaw);
      }
      const res = await fetch('/research/youtube/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to run research');
      }
      this.state.selectedResearchRunId = data.id;
      this.state.selectedResearchRunDetail = data;
      this.renderResearchRunDetail(data);
      if (data.setup_required) {
        this.log(data.setup_message || 'Research setup required.', 'error');
      } else if (data.status === 'failed') {
        this.log(data.setup_message || 'Research run failed.', 'error');
      } else {
        this.log(`Research run #${data.id} completed with ${data.source_video_count || 0} source video(s).`, 'success');
      }
      await this.loadResearchData();
      await this.loadCommandCenter();
      await this.loadExecutiveProducerRecommendation();
      await this.loadProducerHistory();
    } catch (err) {
      this.log(`Run research failed: ${err.message}`, 'error');
    } finally {
      if (button) {
        button.classList.remove('loading');
        button.disabled = false;
      }
    }
  },

  async selectResearchRun(runId) {
    const id = Number(runId);
    if (!Number.isFinite(id) || id <= 0) return;
    this.state.selectedResearchRunId = id;
    this.renderResearchRuns();
    try {
      const res = await fetch(`/research/runs/${id}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load research run detail');
      }
      this.state.selectedResearchRunDetail = data;
      this.renderResearchRunDetail(data);
    } catch (err) {
      this.log(`Research run detail failed: ${err.message}`, 'error');
      this.renderResearchRunDetail(null);
    }
  },

  renderResearchRunDetail(detail) {
    const noticeEl = document.getElementById('researchSetupNotice');
    const sourceEl = document.getElementById('researchSourceVideos');
    const patternEl = document.getElementById('researchPatternCards');
    const createBtn = document.getElementById('btnCreateResearchOpportunities');
    if (!noticeEl || !sourceEl || !patternEl || !createBtn) return;

    if (!detail) {
      noticeEl.textContent = 'Use official YouTube Data API only. Strategy output is original and deterministic.';
      sourceEl.innerHTML = '<div class="empty-state">Select a run to view source videos.</div>';
      patternEl.innerHTML = '<div class="empty-state">Select a run to view extracted patterns.</div>';
      createBtn.disabled = true;
      return;
    }

    if (detail.setup_required) {
      noticeEl.textContent = detail.setup_message || 'Setup required: set YOUTUBE_DATA_API_KEY.';
      noticeEl.style.color = 'var(--warning)';
    } else {
      noticeEl.textContent = 'Use official YouTube Data API only. Strategy output is original and deterministic.';
      noticeEl.style.color = 'var(--textSecondary)';
    }

    const sourceVideos = Array.isArray(detail.source_videos) ? detail.source_videos : [];
    sourceEl.innerHTML = sourceVideos.length > 0
      ? sourceVideos.slice(0, 8).map(video => `
        <div class="video-card" style="padding:0.7rem;">
          <div class="video-title">${this.escapeHtml(video.title || '-')}</div>
          <div class="video-meta-line">${this.escapeHtml(video.channel_title || '-')} • views: ${this.escapeHtml(String(video.view_count ?? '-'))}</div>
          <div class="video-meta-line">${this.escapeHtml(video.duration || 'duration n/a')}</div>
        </div>
      `).join('')
      : '<div class="empty-state">No source videos stored for this run.</div>';

    const patterns = Array.isArray(detail.patterns) ? detail.patterns : [];
    patternEl.innerHTML = patterns.length > 0
      ? patterns.map(pattern => `
        <article class="agent-card">
          <div class="agent-card-header">
            <div class="video-title-main">${this.escapeHtml(pattern.label || pattern.pattern_type || 'Pattern')}</div>
            <span class="video-status ${pattern.signal_strength >= 4 ? 'warning' : 'approved'}">S${this.escapeHtml(String(pattern.signal_strength || 1))}</span>
          </div>
          <div class="video-meta-line">${this.escapeHtml(pattern.pattern_type || '-')}</div>
          <div class="meta-row"><span class="meta-value">${this.escapeHtml(pattern.details || '-')}</span></div>
        </article>
      `).join('')
      : '<div class="empty-state">No patterns stored for this run.</div>';

    createBtn.disabled = !detail.strategy_detail;
  },

  async createOpportunitiesFromResearch() {
    const detail = this.state.selectedResearchRunDetail;
    if (!detail || !detail.id) {
      this.log('Select a research run first.', 'error');
      return;
    }
    const button = document.getElementById('btnCreateResearchOpportunities');
    if (button) {
      button.classList.add('loading');
      button.disabled = true;
    }
    try {
      const res = await fetch(`/research/runs/${detail.id}/create-opportunities`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to create opportunities from strategy');
      }
      this.log(data.message || 'Created opportunities from strategy.', 'success');
      await this.loadOpportunities();
      await this.loadCommandCenter();
      await this.loadPipelineDaily();
      await this.loadExecutiveProducerRecommendation();
      await this.loadProducerHistory();
    } catch (err) {
      this.log(`Create opportunities failed: ${err.message}`, 'error');
    } finally {
      if (button) {
        button.classList.remove('loading');
        button.disabled = false;
      }
    }
  },

  viewRecommendedOpportunity() {
    const recommendation = this.state.producerRecommendation;
    if (!recommendation || !recommendation.selected_opportunity_id) {
      this.log('No recommended opportunity to view.', 'error');
      return;
    }
    this.setActivePage('opportunities');
    this.log(`Opened opportunities for recommended item #${recommendation.selected_opportunity_id}.`, 'info');
  },

  async promoteRecommendedOpportunity() {
    const recommendation = this.state.producerRecommendation;
    if (!recommendation || !recommendation.selected_opportunity_id) {
      this.log('No recommended opportunity to promote.', 'error');
      return;
    }
    if (recommendation.selected_review_status !== 'approved_for_video') {
      this.log('Promotion blocked: recommended opportunity is not approved_for_video.', 'error');
      return;
    }
    await this.promoteOpportunity(recommendation.selected_opportunity_id);
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
    const previewSelectedVideoTitle = document.getElementById('previewSelectedVideoTitle');
    if (previewSelectedVideoTitle) previewSelectedVideoTitle.textContent = video.title;
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
    const previewPanel = document.getElementById('previewPanel');
    if (readinessPanel) readinessPanel.classList.remove('hidden');
    if (publishingSettings) publishingSettings.classList.remove('hidden');
    if (previewPanel) previewPanel.classList.remove('hidden');

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
    await this.loadPreviewStatus();
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
    const previewPanel = document.getElementById('previewPanel');
    const publishingSettings = document.getElementById('publishingSettings');
    const auditPanel = document.getElementById('auditPanel');
    if (selectedVideoActions) selectedVideoActions.style.display = 'none';
    if (metadataDisplay) metadataDisplay.classList.add('hidden');
    if (readinessPanel) readinessPanel.classList.add('hidden');
    if (previewPanel) previewPanel.classList.add('hidden');
    if (publishingSettings) publishingSettings.classList.add('hidden');
    if (auditPanel) auditPanel.classList.add('hidden');
    const ctaPanel = document.getElementById('ctaPanel');
    if (ctaPanel) ctaPanel.innerHTML = '<div class="empty-state" style="height: auto; padding: 1rem;">Select a video to see actions</div>';
    this.renderPreviewStatus(null);
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
    } else if (currentStepIndex === 2 && readiness && !readiness.preview_rendered) {
      ctaPanel.innerHTML = `<button class="btn primary" id="btnDynamic" onclick="app.renderDraftPreview()">Render Draft Preview</button>`;
    } else if (currentStepIndex === 2 && readiness && readiness.preview_rendered && !readiness.preview_reviewed) {
      ctaPanel.innerHTML = `<button class="btn warning" id="btnDynamic" onclick="app.focusPreviewPanel()">Watch Draft Preview</button>`;
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
      keepComplianceReport = false,
      reloadOpportunities = false,
      reloadProducer = true,
      reloadBriefs = true,
      reloadCommandCenter = true,
      reloadPipelineDaily = true
    } = options;

    await this.loadVideos();
    await this.loadPipelineSummary();
    if (reloadCalendar) {
      await this.loadCalendar();
    }
    if (reloadAudit) {
      await this.loadGlobalAudit();
    }
    if (reloadOpportunities) {
      await this.loadOpportunities();
    }
    if (reloadProducer) {
      await this.loadExecutiveProducerRecommendation();
      await this.loadProducerHistory();
    }
    if (reloadBriefs) {
      await this.loadBriefs();
    }
    if (reloadCommandCenter) {
      await this.loadCommandCenter();
    }
    if (reloadPipelineDaily) {
      await this.loadPipelineDaily();
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
      { key: 'preview_rendered', label: 'Preview Rendered' },
      { key: 'preview_reviewed', label: 'Preview Manually Reviewed' },
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
      previewPlaceholderText.textContent = readiness.preview_rendered
        ? (readiness.preview_reviewed ? 'Draft preview reviewed.' : 'Draft preview exists but still needs manual review.')
        : 'No rendered video preview yet.';
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

  async loadPreviewStatus() {
    const selected = this.getSelectedVideo();
    if (!selected) {
      this.renderPreviewStatus(null);
      return null;
    }
    try {
      const res = await fetch(`/videos/${selected.id}/preview/status`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to load preview status');
      }
      this.state.previewStatusByVideoId[selected.id] = data;
      this.renderPreviewStatus(data);
      return data;
    } catch (err) {
      this.log(`Preview status error: ${err.message}`, 'error');
      this.renderPreviewStatus(null);
      return null;
    }
  },

  renderPreviewStatus(status) {
    const statusText = document.getElementById('previewStatusText');
    const expectedPath = document.getElementById('previewExpectedPath');
    const reviewedText = document.getElementById('previewReviewedText');
    const audioStatusText = document.getElementById('previewAudioStatusText');
    const ttsProviderText = document.getElementById('previewTtsProviderText');
    const ttsVoiceModelText = document.getElementById('previewTtsVoiceModelText');
    const ttsSetupHintText = document.getElementById('previewTtsSetupHintText');
    const previewPlayer = document.getElementById('previewPlayer');
    const missingMessage = document.getElementById('previewMissingMessage');
    const markReviewedBtn = document.getElementById('btnMarkPreviewReviewed');
    if (!statusText || !expectedPath || !reviewedText || !audioStatusText || !ttsProviderText || !ttsVoiceModelText || !ttsSetupHintText || !previewPlayer || !missingMessage || !markReviewedBtn) return;

    if (!status) {
      statusText.textContent = 'No rendered video preview yet.';
      expectedPath.textContent = 'out/previews/{video_id}/draft.mp4';
      reviewedText.textContent = 'Not reviewed';
      previewPlayer.pause();
      previewPlayer.removeAttribute('src');
      previewPlayer.load();
      previewPlayer.classList.add('hidden');
      missingMessage.textContent = 'No rendered video preview yet.';
      missingMessage.classList.remove('hidden');
      markReviewedBtn.disabled = true;
      markReviewedBtn.style.display = 'none';
      audioStatusText.textContent = 'No audio';
      ttsProviderText.textContent = 'Not rendered';
      ttsVoiceModelText.textContent = 'Not rendered';
      ttsSetupHintText.textContent = 'Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID for premium voiceover.';
      return;
    }

    expectedPath.textContent = status.expected_path || 'out/previews/{video_id}/draft.mp4';
    if (status.preview_exists && status.preview_url) {
      const durationLabel = status.duration_seconds ? ` (${Math.round(status.duration_seconds)}s)` : '';
      statusText.textContent = `Preview available${durationLabel}: ${status.preview_path || 'local file'}.`;
      previewPlayer.src = status.preview_url;
      previewPlayer.classList.remove('hidden');
      missingMessage.classList.add('hidden');
      markReviewedBtn.disabled = !!status.preview_reviewed;
      markReviewedBtn.style.display = 'inline-flex';
      reviewedText.textContent = status.preview_reviewed
        ? `Reviewed${status.preview_reviewed_at ? ` at ${new Date(status.preview_reviewed_at).toLocaleString()}` : ''}`
        : 'Not reviewed yet';
      if (status.audio_generated) {
        audioStatusText.textContent = `Voiceover generated${status.voiceover_path ? ` (${status.voiceover_path})` : ''}.`;
      } else {
        audioStatusText.textContent = status.silent_reason || 'Silent draft preview generated.';
      }
      ttsProviderText.textContent = status.tts_provider || 'Unknown';
      ttsVoiceModelText.textContent = `${status.tts_voice || 'n/a'} / ${status.tts_model || 'n/a'}`;
      if (status.tts_provider === 'elevenlabs') {
        ttsSetupHintText.textContent = 'Premium ElevenLabs voiceover active.';
      } else if (status.tts_provider === 'openai') {
        ttsSetupHintText.textContent = 'Using OpenAI TTS fallback. Configure ElevenLabs vars for premium voice.';
      } else {
        ttsSetupHintText.textContent = 'Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID for premium voiceover.';
      }
    } else {
      statusText.textContent = 'No rendered video preview yet.';
      previewPlayer.pause();
      previewPlayer.removeAttribute('src');
      previewPlayer.load();
      previewPlayer.classList.add('hidden');
      missingMessage.textContent = 'No rendered video preview yet.';
      missingMessage.classList.remove('hidden');
      reviewedText.textContent = 'Not reviewed';
      markReviewedBtn.disabled = true;
      markReviewedBtn.style.display = 'none';
      audioStatusText.textContent = 'No audio';
      ttsProviderText.textContent = 'Not rendered';
      ttsVoiceModelText.textContent = 'Not rendered';
      ttsSetupHintText.textContent = 'Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID for premium voiceover.';
    }
  },

  async refreshPreviewStatus() {
    await this.loadPreviewStatus();
  },

  async renderDraftPreview() {
    const selected = this.requireSelectedVideo('render draft preview');
    if (!selected) return;

    const btn = document.getElementById('btnRenderDraftPreview');
    if (btn) {
      btn.classList.add('loading');
      btn.disabled = true;
    }
    try {
      const res = await fetch(`/videos/${selected.id}/preview/render-draft`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to render draft preview');
      }
      this.log(
        data.audio_generated
          ? 'Draft preview rendered with voiceover.'
          : `Draft preview rendered without voiceover. ${data.silent_reason || ''}`.trim(),
        'success'
      );
      this.state.previewStatusByVideoId[selected.id] = data;
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: false, reloadAudit: true, keepComplianceReport: true });
      this.focusPreviewPanel();
    } catch (err) {
      this.log(`Render draft preview failed: ${err.message}`, 'error');
    } finally {
      if (btn) {
        btn.classList.remove('loading');
        btn.disabled = false;
      }
    }
  },

  async markPreviewReviewed() {
    const selected = this.requireSelectedVideo('mark draft preview reviewed');
    if (!selected) return;
    const btn = document.getElementById('btnMarkPreviewReviewed');
    if (btn) {
      btn.classList.add('loading');
      btn.disabled = true;
    }
    try {
      const res = await fetch(`/videos/${selected.id}/preview/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed: true })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to mark preview reviewed');
      }
      this.log('Draft preview marked as reviewed.', 'success');
      this.state.previewStatusByVideoId[selected.id] = data;
      await this.refreshAfterMutation({ reloadAssets: false, reloadCalendar: false, reloadAudit: true, keepComplianceReport: true });
      this.focusPreviewPanel();
    } catch (err) {
      this.log(`Mark preview reviewed failed: ${err.message}`, 'error');
    } finally {
      if (btn) {
        btn.classList.remove('loading');
        btn.disabled = false;
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
