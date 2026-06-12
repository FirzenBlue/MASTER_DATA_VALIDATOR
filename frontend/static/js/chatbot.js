(function () {
  let hasStarted = false;

  function createChatbot() {
    const wrapper = document.createElement("div");

    wrapper.innerHTML = `
      <button class="mdv-chatbot-button" id="mdvChatbotButton">
      <img src="/static/assets/bot_11306159.png" alt="Chat" style="width: 100%; height: 100%; border-radius: 50%; object-fit: cover;" />
      </button>

      <div class="mdv-chatbot-window" id="mdvChatbotWindow">
        <div class="mdv-chatbot-header">
          <div class="mdv-chatbot-logo"></div>
          <div class="mdv-chatbot-title">
            <strong>MDV Chatbot</strong>
          </div>          
            <button class="mdv-chatbot-reload" id="mdvChatbotReload" title="Restart Chat"><img src="/static/assets/refresh_17780089.png" alt="Reload" style="width: 16px; height: 16px;" /></button>
            <button class="mdv-chatbot-close" id="mdvChatbotClose"><img src="/static/assets/icons8-close-96.png" alt="Close" style="width: 16px; height: 16px;" /></button>
        </div>


        <div class="mdv-chatbot-body" id="mdvChatbotBody"></div>

        <div class="mdv-chatbot-footer">
          <input class="mdv-chatbot-input" id="mdvChatbotInput" placeholder="Your question here..." />
          <button class="mdv-chatbot-send" id="mdvChatbotSend">
          <img src="/static/assets/send_9572674.png" alt="Send" style="width: 20px; height: 20px;" />
          </button>
        </div>
      </div>
    `;

    document.body.appendChild(wrapper);

    document.getElementById("mdvChatbotButton").addEventListener("click", openChatbot);
    document.getElementById("mdvChatbotClose").addEventListener("click", closeChatbot);
    document.getElementById("mdvChatbotReload").addEventListener("click", reloadChatbot);
    document.getElementById("mdvChatbotSend").addEventListener("click", handleInput);

    document.getElementById("mdvChatbotInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        handleInput();
      }
    });
  }

  function openChatbot() {
    document.getElementById("mdvChatbotWindow").classList.add("open");

    if (!hasStarted) {
      hasStarted = true;
      addMessage("I am your MDV Chatbot. You can ask me any questions", "bot");
      addMessage("For example: What is material master? What is BOM? What is MATNR?", "bot");
    }
  }

  function reloadChatbot() {
    document.getElementById("mdvChatbotBody").innerHTML = "";
    hasStarted = false;
    openChatbot();
  }

  function closeChatbot() {
    document.getElementById("mdvChatbotWindow").classList.remove("open");
  }

  function removeLastThinkingMessage() {
    const body = document.getElementById("mdvChatbotBody");
    const messages = body.querySelectorAll(".mdv-message.bot");
    const lastMessage = messages[messages.length - 1];

    if (lastMessage && lastMessage.textContent === "Thinking...") {
      lastMessage.remove();
    }
  }

  function addMessage(text, sender) {
    const body = document.getElementById("mdvChatbotBody");
    const message = document.createElement("div");

    message.className = `mdv-message ${sender}`;

    if (sender === "bot" && typeof marked !== 'undefined') {
      message.innerHTML = marked.parse(text);
    }
    else{
      message.textContent = text;
    }
    body.appendChild(message);
    body.scrollTop = body.scrollHeight;
  }

  function setInputDisabled(disabled) {
    document.getElementById("mdvChatbotInput").disabled = disabled;
    document.getElementById("mdvChatbotSend").disabled = disabled;
  }

  async function handleInput() {
    const input = document.getElementById("mdvChatbotInput");
    const value = input.value.trim();

    if (!value) return;

    input.value = "";
    addMessage(value, "user");

    const body = document.getElementById("mdvChatbotBody");
    const loadingMessage = document.createElement("div");
    loadingMessage.className = "mdv-message bot loading-indicator"; // added a distinct class
    loadingMessage.textContent = "Thinking...";
    body.appendChild(loadingMessage);
    body.scrollTop = body.scrollHeight;

    setInputDisabled(true);

    try {
      const response = await fetch("/api/chatbot/message", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message: value
        })
      });

      const data = await response.json();

      if (loadingMessage) {
        loadingMessage.remove();
      }

      if (!response.ok) {
        throw new Error(data.detail || "Chatbot request failed");
      }

      addMessage(data.reply, "bot");
    } catch (error) {
      if (loadingMessage) {
        loadingMessage.remove();
      }
      addMessage("Sorry, I could not get an answer right now. Please try again.", "bot");
    } finally {
      setInputDisabled(false);
      input.focus();
    }
  }

  

  document.addEventListener("DOMContentLoaded", createChatbot);
})();