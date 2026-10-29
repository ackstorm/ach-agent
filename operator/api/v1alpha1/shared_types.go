// SPDX-License-Identifier: MIT

package v1alpha1

// SecretKeyRef identifies a key in a same-namespace Secret.
type SecretKeyRef struct {
	// Name is the Kubernetes Secret name.
	//
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Key is the data key inside the Secret.
	//
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Key string `json:"key"`
}
