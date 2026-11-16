const status = document.getElementById('status');
let popupWindowId = null;
function render(result) {
  if (result.error) status.textContent = result.error;
  else if (!result.connected) status.textContent = 'Подключение восстанавливается…';
  else if (result.windowId === null) status.textContent = result.sessionCount ? 'Подключено · сессий Codex: ' + result.sessionCount : 'Подключено · группа ещё не создана';
  else if (result.groupId === null) status.textContent = (result.windowId === popupWindowId ? 'Окно выбрано' : 'Выбрано другое окно') + ' · группа появится при открытии сайта';
  else status.textContent = (result.windowId === popupWindowId ? 'Группа в этом окне' : 'Группа в другом окне') + ' · вкладок: ' + result.tabs.length;
  const items = (result.tabs || []).map(tab => {
    const item = document.createElement('li');
    item.textContent = (tab.selected ? '● ' : '') + (tab.title || tab.url || 'Загрузка…');
    item.title = tab.url;
    return item;
  });
  document.getElementById('tabs').replaceChildren(...items);
}
async function send(action) {
  try {
    const window = await chrome.windows.getCurrent();
    popupWindowId = window.id;
    render(await chrome.runtime.sendMessage({action, windowId: window.id}));
  }
  catch (error) {status.textContent = error.message;}
}
void send('status');
for (const action of ['bind-window', 'add-current', 'release']) {
  document.getElementById(action).onclick = async event => {
    event.target.disabled = true;
    status.textContent = 'Выполняю…';
    try {await send(action);} finally {event.target.disabled = false;}
  };
}
