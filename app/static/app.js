const app = {
  state: {
    videos: [],
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
    await this.loadVideos();
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

  async loadVideos() {
    try {
      const btn = document.getElementById('btnRefreshVideos');
      btn.textContent = 'Loading...';
      btn.disabled = true;

      const res = await fetch('/videos');
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
      btn.textContent = 'Refresh';
      btn.disabled = false;
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

    if (fetchAssets) {
      await this.loadAssets();
    }
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
      this.closeNewIdeaModal();
      await this.loadVideos();
    } catch (err) {
      this.log(`Error creating idea: ${err.message}`, 'error');
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
      this.closeEditModal();
      await this.loadVideos();
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
      this.closeDeleteModal();
      this.state.selectedVideoId = null;
      document.getElementById('selectedVideoTitle').textContent = 'Selected Video: None';
      document.getElementById('selectedVideoActions').style.display = 'none';
      document.getElementById('metadataDisplay').classList.add('hidden');
      document.getElementById('ctaPanel').innerHTML = '<div class="empty-state" style="height: auto; padding: 1rem;">Select a video to see actions</div>';
      
      await this.loadVideos();
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
      this.closeEditAssetModal();
      
      // Reload videos and assets to reflect status change to needs_review
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
      
      // Reload videos and assets to reflect status change to needs_review
      await this.loadVideos();
      await this.loadAssets();
    } catch (err) {
      this.log(`Error regenerating asset: ${err.message}`, 'error');
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
