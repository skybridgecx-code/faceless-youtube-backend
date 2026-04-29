const app = {
  state: {
    videos: [],
    calendarVideos: [],
    auditEvents: [],
    videoAuditEvents: [],
    operatorExport: null,
    selectedVideoId: null,
    assets: [],
    selectedAssetType: null,
    editingAssetType: null,
    stats: {
      ideas: 0,
      generated: 0,
      approved: 0,
      packages: 0
    }
  },

  async init() {
    this.log('Initializing Local AI Operator Dashboard...', 'info');
    await this.checkHealth();
    await this.loadPipelineSummary();
    await this.loadVideos();
    await this.loadCalendar();
    await this.loadGlobalAudit();
  },

  log(msg, type = 'info') {
    const logContainer = document.getElementById('activityLog');
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    
    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="log-time">[${time}]</span><span class="log-msg">${msg}</span>`;
    
    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;
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
      this.log(`Loaded ${data.length} videos`, 'success');

      if (this.state.selectedVideoId) {
        this.selectVideo(this.state.selectedVideoId, false);
      }
    } catch (err) {
      this.log(`Error loading videos: ${err.message}`, 'error');
      document.getElementById('videoList').innerHTML = `<div class="empty-state">Failed to load videos.</div>`;
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

    const queueList = document.getElementById('actionQueueList');
    if (queueList) {
      queueList.innerHTML = '';
      if (!data.action_queue || data.action_queue.length === 0) {
        queueList.innerHTML = `<div class="empty-state">Pipeline is clear!</div>`;
        return;
      }
      data.action_queue.forEach(action => {
        const item = document.createElement('div');
        item.className = 'video-card';
        item.style.padding = '0.75rem';
        item.onclick = () => this.selectVideo(action.video_id);
        
        let color = 'var(--primary)';
        if (action.publish_status === 'blocked') color = 'var(--danger)';
        else if (action.workflow_status === 'needs_review') color = 'var(--warning)';
        
        item.innerHTML = `
          <div style="font-weight: 500; font-size: 0.95rem; margin-bottom: 0.25rem;">${action.title}</div>
          <div style="font-size: 0.8rem; color: var(--textSecondary); margin-bottom: 0.5rem;">${action.reason}</div>
          <div style="font-size: 0.8rem; color: ${color}; font-weight: 500;">→ ${action.suggested_next_action}</div>
        `;
        queueList.appendChild(item);
      });
    }
  },

  updateKPIs() {
    let ideas = this.state.videos.length;
    let approved = this.state.videos.filter(v => v.approved).length;
    let generated = this.state.videos.filter(v => v.status === 'generated' || v.status === 'approved' || v.status === 'packaged').length;
    let packages = this.state.videos.filter(v => v.status === 'packaged').length;

    document.getElementById('kpiTotalIdeas').textContent = ideas;
    document.getElementById('kpiGeneratedAssets').textContent = generated;
    document.getElementById('kpiApprovedVideos').textContent = approved;
    document.getElementById('kpiPackagesCreated').textContent = packages;
  },

  renderVideoList() {
    const list = document.getElementById('videoList');
    list.innerHTML = '';

    if (this.state.videos.length === 0) {
      list.innerHTML = `<div class="empty-state">No video ideas found.</div>`;
      return;
    }

    this.state.videos.forEach(video => {
      const card = document.createElement('div');
      card.className = `video-card ${this.state.selectedVideoId === video.id ? 'selected' : ''}`;
      card.onclick = () => this.selectVideo(video.id);

      const statusLabel = video.status || 'idea';

      card.innerHTML = `
        <div class="video-header">
          <div class="video-title">${video.title}</div>
          <div class="video-status ${statusLabel}">${statusLabel}</div>
        </div>
        <div class="video-details">
          <div class="detail-item">
            <span class="detail-label">Pillar</span>
            <span class="detail-value">${video.pillar || 'N/A'}</span>
          </div>
          <div class="detail-item">
            <span class="detail-label">Target Viewer</span>
            <span class="detail-value">${video.target_viewer || 'N/A'}</span>
          </div>
          <div class="detail-item" style="grid-column: span 2; margin-top: 4px;">
            <span class="detail-label">Pain Point</span>
            <span class="detail-value">${video.pain_point || 'N/A'}</span>
          </div>
        </div>
      `;
      list.appendChild(card);
    });
  },

  async selectVideo(id, fetchAssets = true) {
    this.state.selectedVideoId = id;
    this.renderVideoList();
    this.renderCalendar();

    const video = this.state.videos.find(v => v.id === id);
    if (!video) return;

    document.getElementById('selectedVideoTitle').textContent = `Selected: ${video.title}`;
    
    // Show actions and metadata display
    document.getElementById('selectedVideoActions').style.display = 'flex';
    document.getElementById('metadataDisplay').classList.remove('hidden');

    const emptyText = '<span class="empty-state-text">Not set</span>';
    document.getElementById('metaNiche').innerHTML = video.niche || emptyText;
    document.getElementById('metaAudience').innerHTML = video.target_audience || emptyText;
    document.getElementById('metaAngle').innerHTML = video.angle || emptyText;
    document.getElementById('metaNotes').innerHTML = video.notes || emptyText;

    // Show Publishing and Readiness panels
    document.getElementById('readinessPanel').classList.remove('hidden');
    document.getElementById('publishingSettings').classList.remove('hidden');
    
    // Populate Publishing Settings
    document.getElementById('pubStatus').value = video.publish_status || 'draft';
    
    if (video.publish_date) {
      const d = new Date(video.publish_date);
      d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
      document.getElementById('pubDate').value = d.toISOString().slice(0, 16);
    } else {
      document.getElementById('pubDate').value = '';
    }
    
    document.getElementById('pubNotes').value = video.publish_notes || '';
    document.getElementById('pubError').style.display = 'none';

    // Show Compliance Panel
    document.getElementById('compliancePanel').classList.remove('hidden');
    document.getElementById('auditPanel').classList.remove('hidden');
    this.clearComplianceResults();

    if (fetchAssets) {
      await this.loadAssets();
    }
    
    await this.loadReadiness();
    await this.loadVideoAudit();
    this.updateWorkflowAndCTA(video);
  },

  updateWorkflowAndCTA(video) {
    const steps = ['idea', 'generated', 'approved', 'packaged', 'payload_ready'];
    
    let currentStepIndex = 0;
    if (video.status === 'generated') currentStepIndex = 1;
    if (video.approved || video.status === 'approved') currentStepIndex = 2;
    if (video.status === 'packaged') currentStepIndex = 3;
    if (video.status === 'payload_ready') currentStepIndex = 4;

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
    } else if (currentStepIndex === 2) {
      ctaPanel.innerHTML = `<button class="btn primary" id="btnDynamic" onclick="app.packageAssets()">Package</button>`;
    } else if (currentStepIndex === 3) {
      ctaPanel.innerHTML = `<button class="btn primary" id="btnDynamic" onclick="app.prepareYtPayload()">Prepare YouTube Payload</button>`;
    } else if (currentStepIndex >= 4) {
      ctaPanel.innerHTML = `<button class="btn success" id="btnDynamic" disabled>Workflow Complete</button>`;
    }

    // Update Package Info display
    const packageInfo = document.getElementById('packageInfo');
    if (video.package_dir) {
      packageInfo.classList.remove('hidden');
      document.getElementById('packagePathText').textContent = video.package_dir;
      document.getElementById('openCommandText').textContent = `open "${video.package_dir}"`;
    } else {
      packageInfo.classList.add('hidden');
    }
  },

  async actionWrapper(btnId, actionName, fetchOptions, urlFn, successMsgFn) {
    if (!this.state.selectedVideoId) return;
    
    const btn = document.getElementById(btnId);
    if(btn) {
      btn.classList.add('loading');
      btn.disabled = true;
    }

    try {
      this.log(`Starting: ${actionName}...`, 'info');
      const url = urlFn(this.state.selectedVideoId);
      const res = await fetch(url, fetchOptions);
      
      const data = await res.json();
      
      if (!res.ok) {
        throw new Error(data.detail || `Request failed with status ${res.status}`);
      }

      this.log(successMsgFn ? successMsgFn(data) : `${actionName} completed successfully.`, 'success');
      
      await this.loadVideos();
      return data;
    } catch (err) {
      this.log(`${actionName} failed: ${err.message}`, 'error');
    } finally {
      if(btn) {
        btn.classList.remove('loading');
        btn.disabled = false;
      }
    }
  },

  generateAll() {
    this.actionWrapper(
      'btnDynamic',
      'Generate All Assets',
      { method: 'POST' },
      id => `/videos/${id}/generate`,
      () => 'Generated all assets.'
    ).then(() => this.selectVideo(this.state.selectedVideoId));
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
    // Reset checkboxes
    document.querySelectorAll('.check-item input[type="checkbox"]').forEach(cb => cb.checked = false);
    document.getElementById('btnConfirmApprove').disabled = true;
    document.getElementById('reviewModal').classList.remove('hidden');
  },

  closeReviewModal() {
    document.getElementById('reviewModal').classList.add('hidden');
  },

  checkApprovalReady() {
    const checkboxes = document.querySelectorAll('.check-item input[type="checkbox"]');
    const allChecked = Array.from(checkboxes).every(cb => cb.checked);
    document.getElementById('btnConfirmApprove').disabled = !allChecked;
  },

  confirmApprove() {
    this.closeReviewModal();
    this.actionWrapper(
      'btnDynamic',
      'Approve Video',
      { method: 'POST' },
      id => `/videos/${id}/review`,
      () => 'Video approved for packaging.'
    );
  },

  async loadReadiness() {
    if (!this.state.selectedVideoId) return;
    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}/readiness`);
      if (!res.ok) throw new Error('Failed to load readiness');
      const data = await res.json();
      this.renderReadiness(data);
    } catch (err) {
      this.log(`Error loading readiness: ${err.message}`, 'error');
    }
  },

  renderReadiness(readiness) {
    const list = document.getElementById('readinessChecklist');
    const warningsBox = document.getElementById('readinessWarnings');
    
    list.innerHTML = '';
    warningsBox.innerHTML = '';

    const checks = [
      { key: 'has_assets', label: 'Assets Generated' },
      { key: 'is_approved', label: 'Manually Reviewed & Approved' },
      { key: 'is_packaged', label: 'Assets Packaged' },
      { key: 'has_publish_date', label: 'Publish Date Set' }
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

    if (readiness.warnings && readiness.warnings.length > 0) {
      readiness.warnings.forEach(w => {
        const wItem = document.createElement('div');
        wItem.style.color = 'var(--warning)';
        wItem.style.fontSize = '0.85rem';
        wItem.textContent = `⚠️ ${w}`;
        warningsBox.appendChild(wItem);
      });
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
    if (!this.state.selectedVideoId) return;
    
    const btn = document.getElementById('btnRunCompliance');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Running...';
    }

    try {
      this.log('Running compliance checks...', 'info');
      const res = await fetch(`/videos/${this.state.selectedVideoId}/compliance/run`, {
        method: 'POST'
      });
      const data = await res.json();
      
      if (!res.ok) {
        throw new Error(data.detail || 'Compliance check failed');
      }

      this.log(`Compliance check completed: ${data.overall_status}`, 'success');
      
      this.renderComplianceReport(data);
      document.getElementById('complianceStaleNotice').style.display = 'none';
      await this.loadReadiness();
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
    if (!this.state.selectedVideoId) return;
    
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
      const res = await fetch(`/videos/${this.state.selectedVideoId}/publishing`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      
      const data = await res.json();
      
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to update publishing settings');
      }
      
      this.log('Publishing settings saved successfully.', 'success');
      await this.refreshAuditPanels();
      await this.loadVideos();
      await this.loadCalendar();
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
      () => 'Video assets packaged successfully.'
    );
  },

  prepareYtPayload() {
    this.actionWrapper(
      'btnDynamic',
      'Prepare YT Payload',
      { method: 'POST' },
      id => `/publish/${id}/prepare-youtube-payload`,
      () => 'YouTube payload prepared successfully.'
    );
  },

  async copyPackagePath() {
    const text = document.getElementById('packagePathText').textContent;
    await this.copyToClipboard(text, 'Package path copied to clipboard.');
  },

  async copyOpenCommand() {
    const text = document.getElementById('openCommandText').textContent;
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
      await this.refreshAuditPanels();
      this.closeNewIdeaModal();
      await this.loadVideos();
      await this.loadPipelineSummary();
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
      await this.refreshAuditPanels();
      this.closeBatchModal();
      await this.loadVideos();
      await this.loadPipelineSummary();
    } catch (err) {
      this.log(`Error importing batch: ${err.message}`, 'error');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Import Batch';
    }
  },

  openEditModal() {
    if (!this.state.selectedVideoId) return;
    const video = this.state.videos.find(v => v.id === this.state.selectedVideoId);
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
    if (!this.state.selectedVideoId) return;

    const payload = {
      title: document.getElementById('editTitle').value.trim() || null,
      niche: document.getElementById('editNiche').value.trim() || null,
      target_audience: document.getElementById('editAudience').value.trim() || null,
      angle: document.getElementById('editAngle').value.trim() || null,
      notes: document.getElementById('editNotes').value.trim() || null
    };

    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error('Failed to update video');
      this.log('Updated video metadata', 'success');
      await this.refreshAuditPanels();
      this.closeEditModal();
      await this.loadVideos();
      await this.loadPipelineSummary();
    } catch (err) {
      this.log(`Error updating video: ${err.message}`, 'error');
    }
  },

  openDeleteModal() {
    if (!this.state.selectedVideoId) return;
    document.getElementById('deleteModal').classList.remove('hidden');
  },

  closeDeleteModal() {
    document.getElementById('deleteModal').classList.add('hidden');
  },

  async confirmDelete() {
    if (!this.state.selectedVideoId) return;
    
    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Failed to delete video');
      
      this.log('Deleted video idea', 'success');
      await this.refreshAuditPanels();
      this.closeDeleteModal();
      this.state.selectedVideoId = null;
      document.getElementById('selectedVideoTitle').textContent = 'Selected Video: None';
      document.getElementById('selectedVideoActions').style.display = 'none';
      document.getElementById('metadataDisplay').classList.add('hidden');
      document.getElementById('readinessPanel').classList.add('hidden');
      document.getElementById('publishingSettings').classList.add('hidden');
      document.getElementById('ctaPanel').innerHTML = '<div class="empty-state" style="height: auto; padding: 1rem;">Select a video to see actions</div>';
      
      await this.loadVideos();
      await this.loadPipelineSummary();
    } catch (err) {
      this.log(`Error deleting video: ${err.message}`, 'error');
    }
  },

  openEditAssetModal(assetType) {
    if (!this.state.selectedVideoId) return;
    const asset = this.state.assets.find(a => a.asset_type === assetType);
    if (!asset) return;

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
    if (!this.state.selectedVideoId || !this.state.editingAssetType) return;
    
    const bodyText = document.getElementById('editAssetBody').value;
    const assetType = this.state.editingAssetType;

    const payload = {
      body: bodyText
    };

    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}/assets/${assetType}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      
      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to update asset');
      }
      
      this.log(`Asset ${assetType} updated successfully`, 'success');
      await this.refreshAuditPanels();
      this.closeEditAssetModal();
      
      // Reload videos and assets to reflect status change to needs_review
      document.getElementById('complianceStaleNotice').style.display = 'inline';
      await this.loadVideos();
      await this.loadAssets();
    } catch (err) {
      this.log(`Error updating asset: ${err.message}`, 'error');
    }
  },

  async regenerateAsset(assetType) {
    if (!this.state.selectedVideoId) return;
    
    if (!confirm(`Are you sure you want to regenerate the ${assetType} asset? This will reset the video review status.`)) {
      return;
    }

    try {
      this.log(`Regenerating asset ${assetType}...`, 'info');
      const res = await fetch(`/videos/${this.state.selectedVideoId}/assets/${assetType}/regenerate`, {
        method: 'POST'
      });
      
      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to regenerate asset');
      }
      
      this.log(`Asset ${assetType} regenerated successfully`, 'success');
      await this.refreshAuditPanels();
      
      // Reload videos and assets to reflect status change to needs_review
      document.getElementById('complianceStaleNotice').style.display = 'inline';
      await this.loadVideos();
      await this.loadAssets();
    } catch (err) {
      this.log(`Error regenerating asset: ${err.message}`, 'error');
    }
  },


  async loadGlobalAudit() {
    const container = document.getElementById('globalAuditList');
    if (!container) return;

    try {
      const res = await fetch('/audit?limit=25');
      if (!res.ok) throw new Error('Failed to load audit history');
      const events = await res.json();
      this.state.auditEvents = events;
      this.renderGlobalAudit();
    } catch (err) {
      container.innerHTML = `<div class="empty-state">Failed to load history.</div>`;
      this.log(`Audit history failed: ${err.message}`, 'error');
    }
  },

  renderGlobalAudit() {
    const container = document.getElementById('globalAuditList');
    if (!container) return;

    if (!this.state.auditEvents || this.state.auditEvents.length === 0) {
      container.innerHTML = `<div class="empty-state">No operator history yet.</div>`;
      return;
    }

    container.innerHTML = this.state.auditEvents.map(event => {
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
    if (!this.state.selectedVideoId) return;

    const preview = document.getElementById('operatorExportPreview');
    preview.textContent = 'Loading operator summary...';
    document.getElementById('operatorExportModal').classList.remove('hidden');

    try {
      const res = await fetch(`/videos/${this.state.selectedVideoId}/operator-export`);
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
