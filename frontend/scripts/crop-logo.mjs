import sharp from 'sharp';
import fetch from 'node-fetch';
import fs from 'fs';
import path from 'path';

async function cropLogo() {
  try {
    // Fetch the logo from the URL
    const url = 'https://hebbkx1anhila5yf.public.blob.vercel-storage.com/Ooredoo-Oman-Logo-vC0CHLocMxB2ByH2Y8h4fHjF0e3Dhc.png';
    const response = await fetch(url);
    const buffer = await response.buffer();

    // Get image metadata
    const metadata = await sharp(buffer).metadata();
    console.log('Original dimensions:', metadata.width, 'x', metadata.height);

    // Crop the image to remove excess whitespace
    // The logo circles are the main content, so we crop to focus on them
    const croppedBuffer = await sharp(buffer)
      .extract({
        left: Math.floor(metadata.width * 0.1),      // Remove 10% from left
        top: Math.floor(metadata.height * 0.25),     // Remove 25% from top
        width: Math.floor(metadata.width * 0.8),     // Keep 80% width
        height: Math.floor(metadata.height * 0.5)    // Keep 50% height
      })
      .toBuffer();

    // Save the cropped logo
    const outputPath = path.join(process.cwd(), 'public', 'ooredoo-logo.png');
    fs.writeFileSync(outputPath, croppedBuffer);

    const newMetadata = await sharp(croppedBuffer).metadata();
    console.log('Cropped dimensions:', newMetadata.width, 'x', newMetadata.height);
    console.log('Logo cropped and saved successfully!');
  } catch (error) {
    console.error('Error cropping logo:', error);
  }
}

cropLogo();
