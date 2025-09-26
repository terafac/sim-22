# AI Pong Competition Guide 🏆

Welcome, competitors! This guide will explain how to connect your trained AI model to the Pong game environment. Your goal is to create an AI that can receive real-time game state information and send back paddle movement commands to win the match.

---

## 🏁 The Goal

You will write a script that acts as a WebSocket client. This script will:
1.  **Connect** to the game server.
2.  **Receive**  messages containing image in **base64** encoded format.
3.  **Process** this information with your trained model.
4.  **Send** an `ai_prediction` message back to the server with the calculated target Y-coordinate for your paddle.

---

## 🛠️ How to Compete: Step-by-Step

### Step 1: Run the Environment

First, you need to run the game environment locally to test your model.

1.  **Run the Server:**
    Open a terminal and start the Python server. This server acts as the bridge between the game and your AI script.
    ```bash
    # Make sure you have aiohttp installed (pip install aiohttp)
    python server.py
    ```

2.  **Open the Game:**
    Open the `main.html` file in your web browser. You will see the Pong game interface.

3.  **Set to Remote AI Mode:**
    In the game's right-hand panel, under "AI Control Mode," click the **"Remote AI"** button. This tells the game to wait for instructions from an external source (your script!) instead of using its simple built-in AI.

4.  **Start the Match:**
    Press the **"PRESS ENTER TO START"** button in the game window. The ball will start moving, and the game will begin sending out game state information.

### Step 2: Understand the Communication

Your AI script communicates with the server over WebSockets.

#### For reciving the Base64 image, hit the endpoint of API using 
```bash
curl -X POST http://127.0.0.1:8000/capture -H "Content-Type: application/json" -d "{ \"captureOptions\": { \"format\": \"jpeg\", \"quality\": 0.8 }, \"returnBase64\": true }"
```

#### Sending Your Prediction
```bash
curl -s -X POST http://127.0.0.1:8000/control \
  -H 'Content-Type: application/json' \
  -d '{"paddle":"ai1","y":300,"immediate":true}'
```
Notice the model upon which one you are playing. 

### To get score information.
```bash
curl -s http://127.0.0.1:8000/score
```

### How to Run the Example:
1.  Save the code above as `server.py`.
2.  Install the required library: `pip install websockets`.
3.  Open a **second terminal** (leave the `server.py` terminal running).
4.  Hit the API request using **curl** as given above to get Base64 image. 

---

### ✨ Tips for Success

* **Speed Matters:** The faster your model can process the game state and return a `targetY`, the more responsive your paddle will be.

* **Stay Within Bounds:** The paddle cannot go off-screen. Your `targetY` should ideally be within the range `[paddle_height / 2, canvas_height - paddle_height / 2]`.
* **Test Locally:** Use the provided environment to test and refine your model as much as you want before the final submission.

Good luck!
