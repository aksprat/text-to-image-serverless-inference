// static/app.js
document.addEventListener('DOMContentLoaded', () => {
    let currentImageBlob = null;
    let currentPrompt = '';

    const promptInput = document.getElementById('prompt');
    const generateBtn = document.getElementById('generateBtn');
    const uploadBtn = document.getElementById('uploadBtn');
    const loading = document.getElementById('loading');
    const loadingText = document.getElementById('loadingText');
    const message = document.getElementById('message');
    const imageContainer = document.getElementById('imageContainer');
    const generatedImage = document.getElementById('generatedImage');
    const downloadBtn = document.getElementById('downloadBtn');
    const urlDisplay = document.getElementById('urlDisplay');

    function showMessage(text, type = 'success') {
        message.textContent = text;
        message.className = `message ${type}`;
        message.style.display = 'block';
        setTimeout(() => {
            message.style.display = 'none';
        }, 5000);
    }

    function showLoading(text) {
        loadingText.textContent = text;
        loading.style.display = 'block';
        generateBtn.disabled = true;
        uploadBtn.disabled = true;
    }

    function hideLoading() {
        loading.style.display = 'none';
        generateBtn.disabled = false;
        if (currentImageBlob) {
            uploadBtn.disabled = false;
        }
    }

    generateBtn.addEventListener('click', async () => {
        const prompt = promptInput.value.trim();
        if (!prompt) {
            showMessage('Please enter a prompt to generate an image.', 'error');
            return;
        }

        currentPrompt = prompt;
        showLoading('Generating your amazing image...');
        imageContainer.style.display = 'none';
        urlDisplay.style.display = 'none';

        try {
            const response = await fetch('/generate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ prompt })
            });

            if (response.ok) {
                const blob = await response.blob();
                currentImageBlob = blob;

                const imageUrl = URL.createObjectURL(blob);
                generatedImage.src = imageUrl;
                downloadBtn.href = imageUrl;
                downloadBtn.download = `generated-${Date.now()}.png`;

                imageContainer.style.display = 'block';
                uploadBtn.disabled = false;
                showMessage('Image generated successfully! 🎉');
            } else {
                let errorText = 'Unknown error';
                try {
                    const errorData = await response.json();
                    errorText = errorData.error || JSON.stringify(errorData);
                } catch (e) {
                    errorText = await response.text();
                }
                showMessage(`Error: ${errorText}`, 'error');
            }
        } catch (error) {
            showMessage(`Error: ${error.message}`, 'error');
        } finally {
            hideLoading();
        }
    });

    uploadBtn.addEventListener('click', async () => {
        if (!currentPrompt) {
            showMessage('Please generate an image first.', 'error');
            return;
        }

        showLoading('Uploading to DigitalOcean Spaces...');

        try {
            const response = await fetch('/upload-to-spaces', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ prompt: currentPrompt })
            });

            const data = await response.json();

            if (response.ok && data.success) {
                showMessage('Image uploaded successfully to DigitalOcean Spaces! 🎉');
                urlDisplay.innerHTML = `
                    <strong>Public URL:</strong><br>
                    <a href="${data.url}" target="_blank">${data.url}</a>
                `;
                urlDisplay.style.display = 'block';
            } else {
                showMessage(`Upload failed: ${data.error || JSON.stringify(data)}`, 'error');
            }
        } catch (error) {
            showMessage(`Upload error: ${error.message}`, 'error');
        } finally {
            hideLoading();
        }
    });

    // Enable Enter key to generate (Ctrl+Enter)
    promptInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.ctrlKey) {
            generateBtn.click();
        }
    });

    // Add some example prompts
    const examples = [
        "A majestic dragon flying over a medieval castle at sunset",
        "A cyberpunk cityscape with neon lights reflecting on wet streets",
        "A peaceful zen garden with cherry blossoms and a traditional tea house",
        "An astronaut floating in space with Earth visible in the background",
        "A magical forest with glowing mushrooms and fairy lights"
    ];

    // Add placeholder cycling
    let exampleIndex = 0;
    setInterval(() => {
        if (promptInput === document.activeElement) return; // Don't change if user is typing
        promptInput.placeholder = `Try: "${examples[exampleIndex]}" or describe your own image...`;
        exampleIndex = (exampleIndex + 1) % examples.length;
    }, 4000);
});
