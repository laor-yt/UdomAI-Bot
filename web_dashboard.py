import os
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from user_manager import load_users, toggle_user_status

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Udom AI Bot - User Access Management</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #0b0f19;
      --card-bg: #151c2c;
      --card-border: #232d42;
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --success: #10b981;
      --success-bg: rgba(16, 185, 129, 0.15);
      --danger: #ef4444;
      --danger-bg: rgba(239, 68, 68, 0.15);
      --warning: #f59e0b;
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
    body { background-color: var(--bg-dark); color: var(--text-main); padding: 2rem 1rem; min-height: 100vh; }
    .container { max-width: 1100px; margin: 0 auto; }
    
    header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 2rem; }
    .title-area h1 { font-size: 1.75rem; font-weight: 800; background: linear-gradient(135deg, #a5b4fc, #6366f1); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .title-area p { color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem; }
    .admin-badge { background: #1e293b; border: 1px solid #334155; padding: 0.5rem 1rem; border-radius: 99px; font-weight: 600; font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem; }
    .status-dot { width: 8px; height: 8px; background: var(--success); border-radius: 50%; box-shadow: 0 0 8px var(--success); }

    /* Stats Grid */
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
    .stat-card { background: var(--card-bg); border: 1px solid var(--card-border); padding: 1.25rem; border-radius: 16px; }
    .stat-title { color: var(--text-muted); font-size: 0.85rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-value { font-size: 2rem; font-weight: 800; margin-top: 0.5rem; }

    /* Toolbar */
    .toolbar { display: flex; flex-wrap: wrap; gap: 1rem; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }
    .search-box { flex: 1; min-width: 250px; position: relative; }
    .search-box input { width: 100%; background: var(--card-bg); border: 1px solid var(--card-border); color: #fff; padding: 0.75rem 1rem; border-radius: 12px; font-size: 0.95rem; outline: none; }
    .search-box input:focus { border-color: var(--primary); }
    .filter-tabs { display: flex; gap: 0.5rem; background: var(--card-bg); border: 1px solid var(--card-border); padding: 4px; border-radius: 12px; }
    .tab-btn { background: transparent; border: none; color: var(--text-muted); padding: 0.5rem 1rem; font-weight: 600; font-size: 0.85rem; border-radius: 8px; cursor: pointer; transition: 0.2s; }
    .tab-btn.active { background: var(--primary); color: #fff; }

    /* Table */
    .table-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { background: #1a2336; color: var(--text-muted); font-size: 0.8rem; font-weight: 700; text-transform: uppercase; padding: 1rem 1.25rem; border-bottom: 1px solid var(--card-border); }
    td { padding: 1rem 1.25rem; border-bottom: 1px solid var(--card-border); font-size: 0.9rem; }
    tr:last-child td { border-bottom: none; }
    tr:hover { background: rgba(255, 255, 255, 0.02); }

    .user-info { display: flex; flex-direction: column; }
    .user-name { font-weight: 700; color: #fff; }
    .user-handle { font-size: 0.8rem; color: var(--primary); text-decoration: none; }
    .user-handle:hover { text-decoration: underline; }

    .badge { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.35rem 0.75rem; border-radius: 99px; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; }
    .badge-approved { background: var(--success-bg); color: var(--success); }
    .badge-blocked { background: var(--danger-bg); color: var(--danger); }

    .btn { padding: 0.5rem 1rem; border-radius: 8px; border: none; font-weight: 700; font-size: 0.85rem; cursor: pointer; transition: all 0.2s; }
    .btn-approve { background: var(--success); color: #fff; }
    .btn-approve:hover { opacity: 0.9; }
    .btn-block { background: var(--danger); color: #fff; }
    .btn-block:hover { opacity: 0.9; }

    .empty-state { text-align: center; padding: 3rem 1rem; color: var(--text-muted); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-area">
        <h1>Udom AI Bot Dashboard</h1>
        <p>Manage User Access Permissions & Monitor Bot Usage</p>
      </div>
      <div class="admin-badge">
        <span class="status-dot"></span> Live Control Panel
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-title">Total Registered Users</div>
        <div class="stat-value" id="stat-total">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-title">Approved Users</div>
        <div class="stat-value" style="color: var(--success);" id="stat-approved">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-title">Blocked / Pending</div>
        <div class="stat-value" style="color: var(--danger);" id="stat-blocked">0</div>
      </div>
    </div>

    <div class="toolbar">
      <div class="search-box">
        <input type="text" id="searchInput" placeholder="Search by name, username, or Telegram ID..." onkeyup="filterUsers()">
      </div>
      <div class="filter-tabs">
        <button class="tab-btn active" onclick="setFilter('ALL', this)">All</button>
        <button class="tab-btn" onclick="setFilter('APPROVED', this)">Approved</button>
        <button class="tab-btn" onclick="setFilter('BLOCKED', this)">Blocked</button>
      </div>
    </div>

    <div class="table-card">
      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Telegram ID</th>
            <th>Joined / Active</th>
            <th>Requests</th>
            <th>Status</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="userTableBody">
          <tr><td colspan="6" class="empty-state">Loading users...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    let allUsers = [];
    let currentFilter = 'ALL';

    async function fetchUsers() {
      try {
        const res = await fetch('/api/users');
        allUsers = await res.json();
        renderDashboard();
      } catch (err) {
        console.error("Error fetching users:", err);
      }
    }

    function renderDashboard() {
      const search = document.getElementById('searchInput').value.toLowerCase();
      const tbody = document.getElementById('userTableBody');
      
      let approvedCount = 0;
      let blockedCount = 0;

      allUsers.forEach(u => {
        if (u.status === 'APPROVED') approvedCount++;
        else blockedCount++;
      });

      document.getElementById('stat-total').innerText = allUsers.length;
      document.getElementById('stat-approved').innerText = approvedCount;
      document.getElementById('stat-blocked').innerText = blockedCount;

      const filtered = allUsers.filter(u => {
        const matchesFilter = (currentFilter === 'ALL') || (u.status === currentFilter);
        const nameStr = `${u.first_name || ''} ${u.last_name || ''} ${u.username || ''} ${u.user_id}`.toLowerCase();
        const matchesSearch = nameStr.includes(search);
        return matchesFilter && matchesSearch;
      });

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No matching users found.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(u => {
        const fullName = `${u.first_name || ''} ${u.last_name || ''}`.trim() || 'Anonymous User';
        const handle = u.username ? `<a href="https://t.me/${u.username}" target="_blank" class="user-handle">@${u.username}</a>` : '<span style="color:#6b7280;">No username</span>';
        const isApproved = u.status === 'APPROVED';
        
        return `
          <tr>
            <td>
              <div class="user-info">
                <span class="user-name">${fullName}</span>
                ${handle}
              </div>
            </td>
            <td><code>${u.user_id}</code></td>
            <td style="font-size:0.8rem; color:var(--text-muted);">
              <div>Joined: ${u.joined_at || '-'}</div>
              <div>Active: ${u.last_active || '-'}</div>
            </td>
            <td><strong>${u.request_count || 1}</strong></td>
            <td>
              <span class="badge ${isApproved ? 'badge-approved' : 'badge-blocked'}">
                ${isApproved ? '✅ Approved' : '⛔️ Blocked'}
              </span>
            </td>
            <td>
              <button class="btn ${isApproved ? 'btn-block' : 'btn-approve'}" onclick="toggleUser(${u.user_id})">
                ${isApproved ? '🚫 Block' : '✅ Approve'}
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    async function toggleUser(userId) {
      try {
        const res = await fetch('/api/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user_id: userId })
        });
        if (res.ok) {
          fetchUsers();
        }
      } catch (err) {
        alert("Failed to update status.");
      }
    }

    function setFilter(filter, btn) {
      currentFilter = filter;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderDashboard();
    }

    function filterUsers() {
      renderDashboard();
    }

    fetchUsers();
    setInterval(fetchUsers, 5000);
  </script>
</body>
</html>
"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

class DualStackThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True

class DashboardHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        try:
            if self.path in ['/healthz', '/health', '/ping']:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b"OK")
                return
            elif self.path == '/api/users':
                users_dict = load_users()
                users_list = list(users_dict.values())
                users_list.sort(key=lambda x: x.get('joined_at', ''), reverse=True)
                
                body_bytes = json.dumps(users_list, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
            else:
                body_bytes = HTML_TEMPLATE.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
        except Exception as e:
            print(f"HTTP GET Error: {e}")

    def do_POST(self):
        try:
            if self.path == '/api/toggle':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                data = json.loads(body.decode('utf-8'))
                user_id = data.get('user_id')
                updated_user = toggle_user_status(user_id)
                
                res_bytes = json.dumps({"success": True, "user": updated_user}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(res_bytes)))
                self.end_headers()
                self.wfile.write(res_bytes)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            print(f"HTTP POST Error: {e}")
            try:
                self.send_response(400)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, format, *args):
        return

def run_dashboard_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = DualStackThreadingServer(('0.0.0.0', port), DashboardHandler)
        print(f"🚀 Web Dashboard Threading Server running on port {port}...")
        server.serve_forever()
    except Exception as e:
        print(f"Fatal Web Dashboard Server Error: {e}")
