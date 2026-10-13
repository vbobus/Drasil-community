const form = document.querySelector('#server-form');
const input = document.querySelector('#server-url');
const button = document.querySelector('#connect-button');
const errorMessage = document.querySelector('#error-message');

const initialMessage = new URLSearchParams(window.location.search).get('message');
if (initialMessage) {
  errorMessage.textContent = initialMessage;
  errorMessage.hidden = false;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorMessage.hidden = true;
  button.disabled = true;
  button.textContent = 'Connecting…';

  try {
    await window.drasilDesktop.connect(input.value);
  } catch (error) {
    errorMessage.textContent = error instanceof Error ? error.message : String(error);
    errorMessage.hidden = false;
    button.disabled = false;
    button.textContent = 'Connect securely';
    input.focus();
  }
});
