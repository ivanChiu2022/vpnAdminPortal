

// input your API ID and Rejoin information here
const API_BASE = "https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com";

const users = [];

function renderUsers() {
  const tableBody = document.getElementById("userTableBody");
  const totalUsersEl = document.getElementById("totalUsers");
  const activeUsersEl = document.getElementById("activeUsers");
  const terminatedUsersEl = document.getElementById("terminatedUsers");

  tableBody.innerHTML = "";

  let activeCount = 0;
  let terminatedCount = 0;

  users.forEach((user, index) => {
    const status = user.status || "-";

    if (status === "active") activeCount++;
    if (status === "terminated") terminatedCount++;

    const row = document.createElement("tr");

    row.innerHTML = `
      <td>${user.username || "-"}</td>
      <td>${user.device || "-"}</td>
      <td>${user.ip || "-"}</td>
      <td>${user.public_ip || "-"}</td>
      <td>${user.last_handshake || "-"}</td>
      <td class="${status === "active" ? "status-active" : "status-terminated"}">
        ${status}
      </td>
      <td>
        ${
          status === "active"
            ? `<button onclick="terminateUser(${index})">Terminate</button>`
            : "-"
        }
      </td>
    `;

    tableBody.appendChild(row);
  });

  totalUsersEl.textContent = users.length;
  activeUsersEl.textContent = activeCount;
  terminatedUsersEl.textContent = terminatedCount;
}

async function loadUsers() {
  try {
    const response = await fetch(`${API_BASE}/list-users`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ action: "list_users" })
    });

    if (!response.ok) throw new Error(`HTTP error: ${response.status}`);

    const result = await response.json();

    users.length = 0;

    if (result.users && Array.isArray(result.users)) {
      result.users.forEach(user => users.push(user));
    }

    renderUsers();
  } catch (error) {
    console.error("Error loading users.");
    alert("Failed to load users");
  }
}

async function createUser(username, device) {
  try {
    const response = await fetch(`${API_BASE}/create-user`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        action: "create_user",
        username,
        device
      })
    });

    if (!response.ok) throw new Error(`HTTP error: ${response.status}`);

    const result = await response.json();

    alert(`User ${result.username} created successfully.`);

    document.getElementById("usernameInput").value = "";

    await loadUsers();
  } catch (error) {
    console.error("Error creating user.");
    alert("Failed to create user.");
  }
}

function handleCreateUser() {
  const username = document.getElementById("usernameInput").value.trim();
  const device = document.getElementById("deviceInput").value;

  if (!username) {
    alert("Please enter a username.");
    return;
  }

  createUser(username, device);
}

async function terminateUser(index) {
  const user = users[index];

  if (!user) {
    alert("User not found.");
    return;
  }

  const confirmed = confirm(`Are you sure you want to terminate ${user.username}?`);
  if (!confirmed) return;

  try {
    const response = await fetch(`${API_BASE}/delete-user`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        action: "delete_user",
        username: user.username
      })
    });

    if (!response.ok) throw new Error(`HTTP error: ${response.status}`);

    alert(`User ${user.username} terminated successfully.`);

    await loadUsers();
  } catch (error) {
    console.error("Error terminating user.");
    alert("Failed to terminate user.");
  }
}

loadUsers();