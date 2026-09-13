import type { PlatformUpdater, UpdateStatus } from '@/platform/types';

/**
 * JFW-1: Der Updater ist aus dem Produktprofil entfernt (updater_enabled=false).
 * Es gibt keinen Update-Kanal — die Implementierung meldet fail-closed "kein
 * Kanal" statt einen Fremdserver zu kontaktieren.
 */
class DisabledUpdater implements PlatformUpdater {
  private status: UpdateStatus = {
    checking: false,
    available: false,
    downloading: false,
    installing: false,
    readyToInstall: false,
    error: 'Kein Update-Kanal konfiguriert (JFW-1-Profil)',
  };

  private subscribers: Set<(status: UpdateStatus) => void> = new Set();

  subscribe(callback: (status: UpdateStatus) => void): () => void {
    this.subscribers.add(callback);
    callback(this.status);
    return () => {
      this.subscribers.delete(callback);
    };
  }

  getStatus(): UpdateStatus {
    return { ...this.status };
  }

  async checkForUpdates(): Promise<void> {
    // Fail-closed: kein Kanal, keine Anfrage.
  }

  async downloadAndInstall(): Promise<void> {
    throw new Error('Kein Update-Kanal konfiguriert (JFW-1-Profil)');
  }

  async restartAndInstall(): Promise<void> {
    throw new Error('Kein Update-Kanal konfiguriert (JFW-1-Profil)');
  }
}

export const tauriUpdater: PlatformUpdater = new DisabledUpdater();
