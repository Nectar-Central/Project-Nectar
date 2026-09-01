const axios = require('axios');
const fs = require('fs');
const path = require('path');

const BASE_URL = 'https://netlify.app';
const OUTPUT_DIR = path.join(__dirname, 'downloaded_skins');

// Ensure output folder exists
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR);
}

/**
 * Checks if a remote skin file exists and downloads it if valid.
 * @param {string} filename - The name of the file to check.
 */
async function testAndDownloadSkin(filename) {
    const fileUrl = `${BASE_URL}${filename}`;
    const outputPath = path.join(OUTPUT_DIR, filename);

    try {
        // Send a HEAD request to verify file existence without downloading full data first
        const response = await axios.head(fileUrl);
        
        if (response.status === 200) {
            console.log(`[FOUND] ${filename} - Downloading...`);
            
            // Fetch actual file stream
            const fileStream = await axios({
                method: 'get',
                url: fileUrl,
                responseType: 'stream'
            });

            // Save the file locally
            const writer = fs.createWriteStream(outputPath);
            fileStream.data.pipe(writer);

            return new Promise((resolve, reject) => {
                writer.on('finish', resolve);
                writer.on('error', reject);
            });
        }
    } catch (error) {
        // Axios throws an error for 404 Not Found responses
        if (error.response && error.response.status === 404) {
            // Quietly ignore missing assets to clean up terminal output
            return;
        }
        console.error(`[ERROR] Problem checking ${filename}:`, error.message);
    }
}

/**
 * Main loop running through defined patterns
 */
async function runScanner() {
    console.log(`Starting scan against: ${BASE_URL}\n`);
    
    const maxRange = 60; // Adjust upper range boundary as needed
    const genders = ['Male', 'Female'];

    for (let i = 1; i <= maxRange; i++) {
        for (const gender of genders) {
            // Generate standard format name (e.g., cct_1_Male.png, cct_44_Male.png)
            const filename = `cct_${i}_${gender}.png`;
            
            // Execute checks sequentially to avoid spamming the host infrastructure
            await testAndDownloadSkin(filename);
        }
    }

    console.log(`\nScan complete. Valid assets saved to: ${OUTPUT_DIR}`);
}

runScanner();
